"""Success envelope: data, meta (degraded, warnings) and provenance for every response."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse

from imda.api.deps import RequestContext
from imda.api.serialize import to_jsonable
from imda.domain.calendar import CalendarDataMissing
from imda.domain.fx_service import FX_CALENDAR_OFFICE
from imda.models import Dataset, Provenance, Source, SourceStatus
from imda.sources.fbil.common import BASE_URL as FBIL_BASE_URL
from imda.sources.rbi.fx import FX_URL as RBI_FX_URL
from imda.sources.rbi.holidays import HOLIDAYS_URL as RBI_HOLIDAYS_URL

LOOKBACK_DAYS = 14
_BAD_STATUSES = frozenset({SourceStatus.DEGRADED.value, SourceStatus.BROKEN.value})
_FALLBACK_URLS: Mapping[tuple[Source, Dataset], str] = {
    (Source.RBI, Dataset.OFFICES): RBI_HOLIDAYS_URL,
    (Source.RBI, Dataset.HOLIDAYS): RBI_HOLIDAYS_URL,
    (Source.RBI, Dataset.FX): RBI_FX_URL,
    (Source.FBIL, Dataset.FX): f"{FBIL_BASE_URL}/refrates/fetchfiltered",
    (Source.FBIL, Dataset.MIBOR): f"{FBIL_BASE_URL}/ovnmibor/fetchfiltered",
}


@dataclass(frozen=True, slots=True)
class Used:
    """A (source, dataset) a response drew from.

    ``latest_date`` is the newest data date held for it. Give it for time series so staleness
    can be judged; leave it ``None`` for reference data (offices, holidays).
    """

    source: Source
    dataset: Dataset
    latest_date: dt.date | None = None


@dataclass(frozen=True, slots=True)
class Assessment:
    provenance: tuple[Provenance, ...]
    degraded: bool
    warnings: tuple[str, ...] = field(default=())

    @property
    def any_stale(self) -> bool:
        return any(p.stale for p in self.provenance)


def _dedupe(used: Iterable[Used]) -> list[Used]:
    merged: dict[tuple[Source, Dataset], Used] = {}
    for item in used:
        key = (item.source, item.dataset)
        known = merged.get(key)
        if known is None or (item.latest_date or dt.date.min) > (known.latest_date or dt.date.min):
            merged[key] = item
    return list(merged.values())


def _expected_latest(ctx: RequestContext, warnings: list[str]) -> dt.date | None:
    """The last Mumbai business day before today: what a fresh time series should reach."""
    today = ctx.today()
    try:
        for offset in range(1, LOOKBACK_DAYS + 1):
            day = today - dt.timedelta(days=offset)
            if ctx.calendar.is_business_day(FX_CALENDAR_OFFICE, day):
                return day
    except CalendarDataMissing as exc:
        warnings.append(
            f"staleness not checked: holiday data for {exc.office} {exc.year} is not loaded"
        )
    return None


def assess(ctx: RequestContext, used: Iterable[Used]) -> Assessment:
    """Provenance, degraded flag and warnings for the (source, dataset) pairs in ``used``."""
    items = _dedupe(used)
    warnings: list[str] = []
    health = {
        (str(r["source"]), str(r["dataset"])): str(r["status"]) for r in ctx.store.source_health()
    }
    expected = (
        _expected_latest(ctx, warnings) if any(i.latest_date is not None for i in items) else None
    )
    provenance: list[Provenance] = []
    degraded = False
    for item in items:
        fetch = ctx.store.latest_fetch(item.source, item.dataset)
        stale = bool(expected and item.latest_date and item.latest_date < expected)
        provenance.append(
            Provenance(
                source=item.source,
                dataset=item.dataset,
                source_url=fetch.url if fetch else _FALLBACK_URLS[(item.source, item.dataset)],
                fetched_at=fetch.fetched_at if fetch else None,
                stale=stale,
            )
        )
        status = health.get((item.source.value, item.dataset.value))
        if status in _BAD_STATUSES:
            degraded = True
            warnings.append(
                f"{item.source.value}/{item.dataset.value} is {status}; serving the last good data"
            )
        if stale:
            warnings.append(
                f"{item.source.value}/{item.dataset.value} is stale: latest data is "
                f"{item.latest_date}, expected {expected}"
            )
    return Assessment(tuple(provenance), degraded, tuple(dict.fromkeys(warnings)))


def success(
    ctx: RequestContext,
    data: object,
    *,
    used: Iterable[Used] = (),
    count: int | None = None,
    next_cursor: str | None = None,
    warnings: Sequence[str] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Wrap ``data`` in the standard envelope and serialize it (Decimals as strings)."""
    assessment = assess(ctx, used)
    if count is None:
        count = len(data) if isinstance(data, list | tuple) else 1
    body: dict[str, Any] = {
        "data": data,
        "meta": {
            "count": count,
            "next_cursor": next_cursor,
            "degraded": assessment.degraded,
            "warnings": [*warnings, *assessment.warnings],
        },
        "provenance": assessment.provenance,
    }
    return JSONResponse(content=to_jsonable(body), headers=dict(headers or {}))


def envelope_example(data: object, *, provenance: object = None) -> dict[str, Any]:
    """An OpenAPI response entry carrying an example envelope."""
    example = {
        "data": data,
        "meta": {"count": 1, "next_cursor": None, "degraded": False, "warnings": []},
        "provenance": provenance
        or [
            {
                "source": "fbil",
                "dataset": "fx_reference_rates",
                "source_url": "https://www.fbil.org.in/wasdm/refrates/fetchfiltered",
                "fetched_at": "2026-10-02T05:13:18.119836+00:00",
                "stale": False,
            }
        ],
    }
    return {
        "description": "Success",
        "content": {"application/json": {"example": example}},
    }
