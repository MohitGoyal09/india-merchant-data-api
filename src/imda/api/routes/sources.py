"""``GET /v1/sources/health``: status, drift and freshness for every source dataset."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from imda.api.deps import Ctx
from imda.api.envelope import Used, success
from imda.health.drift import DriftReport
from imda.health.freshness import FreshnessReport, assess_freshness
from imda.models import Dataset, Source, SourceStatus

router = APIRouter(prefix="/v1/sources", tags=["sources"])

RECENT_RUNS = 5
_SEVERITY = {
    SourceStatus.UNKNOWN.value: 0,
    SourceStatus.OK.value: 1,
    SourceStatus.DEGRADED.value: 2,
    SourceStatus.BROKEN.value: 3,
}
_PROVENANCE_PAIRS = frozenset(
    {
        (Source.RBI.value, Dataset.OFFICES.value),
        (Source.RBI.value, Dataset.HOLIDAYS.value),
        (Source.RBI.value, Dataset.FX.value),
        (Source.FBIL.value, Dataset.FX.value),
        (Source.FBIL.value, Dataset.MIBOR.value),
    }
)


def _as_str_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def drift_view(stored: Mapping[str, object] | None) -> dict[str, object] | None:
    """The drift report kept with the health row, plus a one-line summary."""
    if stored is None:
        return None
    raw_changed = stored.get("changed")
    changed = raw_changed if isinstance(raw_changed, dict) else {}
    report = DriftReport(
        drifted=bool(stored.get("drifted")),
        added_keys=tuple(_as_str_list(stored.get("added_keys"))),
        removed_keys=tuple(_as_str_list(stored.get("removed_keys"))),
        changed={
            str(key): (value.get("old"), value.get("new"))
            for key, value in changed.items()
            if isinstance(value, dict)
        },
        note=str(stored["note"]) if stored.get("note") else None,
    )
    return {
        "drifted": report.drifted,
        "added_keys": list(report.added_keys),
        "removed_keys": list(report.removed_keys),
        "changed_keys": sorted(report.changed),
        "note": report.note,
        "summary": report.summary(),
    }


def freshness_view(report: FreshnessReport | None) -> dict[str, object] | None:
    if report is None:
        return None
    return {
        "latest_date": None if report.latest_date is None else report.latest_date.isoformat(),
        "expected_date": report.expected_date.isoformat(),
        "lag_business_days": report.lag_business_days,
        "stale": report.stale,
        "calendar_incomplete": report.calendar_incomplete,
    }


def worst_status(statuses: list[str]) -> str:
    """The most severe status; ``unknown`` when there is nothing to judge."""
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0), default=SourceStatus.UNKNOWN.value)


def _item(
    key: tuple[str, str], row: Mapping[str, Any] | None, fresh: FreshnessReport | None
) -> dict[str, object]:
    row = row or {}
    return {
        "source": key[0],
        "dataset": key[1],
        "status": row.get("status", SourceStatus.UNKNOWN.value),
        "checked_at": row.get("checked_at"),
        "last_success_at": row.get("last_success_at"),
        "last_error_at": row.get("last_error_at"),
        "last_error": row.get("last_error"),
        "drift": drift_view(row.get("drift")),
        "freshness": freshness_view(fresh),
    }


@router.get("/health", summary="Source health: status, drift and freshness")
def sources_health(ctx: Ctx) -> JSONResponse:
    rows = {(str(r["source"]), str(r["dataset"])): r for r in ctx.store.source_health()}
    reports = assess_freshness(ctx.store, ctx.calendar, ctx.now(), ctx.settings)
    fresh = {(r.source.value, r.dataset.value): r for r in reports}
    items = [
        _item(key, rows.get(key), fresh.get(key)) for key in sorted(rows.keys() | fresh.keys())
    ]
    data = {
        "status": worst_status([str(item["status"]) for item in items]),
        "sources": items,
        "last_runs": [
            {
                "run_id": run["run_id"],
                "kind": run["kind"],
                "status": run["status"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
            }
            for run in ctx.store.runs(RECENT_RUNS)
        ],
    }
    stale = [
        f"{r.source.value}/{r.dataset.value} is stale: latest data "
        f"{r.latest_date or 'none'}, expected {r.expected_date}"
        for r in reports
        if r.stale
    ]
    used = [Used(Source(s), Dataset(d)) for (s, d) in sorted(rows) if (s, d) in _PROVENANCE_PAIRS]
    return success(ctx, data, used=used, count=len(items), warnings=stale)
