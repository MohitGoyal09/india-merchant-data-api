"""Dataset loaders shared by ``backfill`` and ``refresh``.

Each loader fetches through an adapter, parses, and stores rows tagged with the ``fetch_id`` of
the exchange that produced them. Loaders raise ``UpstreamError`` / ``ParseError``; the caller
(``common.execute_task``) turns those into source health.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from imda.ingest.common import RunEnv, TaskOutput
from imda.models import Currency, Dataset, FxRate, Holiday, MiborRate, Office, Source
from imda.sources.base import (
    DateRangeQuery,
    HolidayQuery,
    HttpClient,
    ParseError,
    RawPayload,
    SourceAdapter,
)
from imda.sources.fbil.common import chunk_ranges
from imda.sources.rbi.aspnet import extract_select_options
from imda.sources.rbi.holidays import HOLIDAYS_URL, YEAR_SELECT, RbiHolidayAdapter
from imda.sources.rbi.offices import parse_offices
from imda.store.repo import HolidayDiff

RBI_FX_ERAS: tuple[tuple[dt.date, dt.date | None], ...] = (
    (dt.date(2000, 1, 3), dt.date(2018, 7, 24)),
    (dt.date(2022, 4, 12), None),
)
FBIL_FX_START = dt.date(2018, 7, 10)
FBIL_MIBOR_START = dt.date(2015, 7, 22)
HOLIDAYS_FIRST_YEAR = 2001
EVENT_RECENCY_DAYS = 30
"""FX rows older than this never raise ``fx.rates.published`` (a backfill is not news)."""

FxAdapter = SourceAdapter[DateRangeQuery, FxRate]
MiborAdapter = SourceAdapter[DateRangeQuery, MiborRate]


# ---------------------------------------------------------------- ranges
def rbi_fx_ranges(start: dt.date, end: dt.date) -> list[DateRangeQuery]:
    """``[start, end]`` clipped to the eras RBI publishes in (the 2018-2022 gap is skipped)."""
    ranges: list[DateRangeQuery] = []
    for era_start, era_end in RBI_FX_ERAS:
        low, high = max(start, era_start), min(end, era_end or end)
        if low <= high:
            ranges.append(DateRangeQuery(low, high))
    return ranges


def floored_range(start: dt.date, end: dt.date, floor: dt.date) -> list[DateRangeQuery]:
    low = max(start, floor)
    return [DateRangeQuery(low, end)] if low <= end else []


def _windows(ranges: Sequence[DateRangeQuery]) -> Iterator[DateRangeQuery]:
    for window in ranges:
        yield from chunk_ranges(window)


# ---------------------------------------------------------------- offices
def load_offices(env: RunEnv) -> TaskOutput:
    page = env.page.raw(HOLIDAYS_URL)
    offices = parse_offices(page.text())
    changed = env.store.upsert_offices(offices, env.log.fetch_id_for(page))
    return TaskOutput(rows=changed)


# ---------------------------------------------------------------- holidays
@dataclass(frozen=True, slots=True)
class HolidayContext:
    env: RunEnv
    adapter: RbiHolidayAdapter
    client: HttpClient
    offices: tuple[Office, ...]
    max_year: int


@dataclass(frozen=True, slots=True)
class Loaded:
    rows: int
    raw: RawPayload | None


def prepare_holidays(env: RunEnv) -> HolidayContext:
    """Fetch the page once; make sure offices exist (FK) and find RBI's newest year."""
    page = env.page.raw(HOLIDAYS_URL)
    offices = env.store.offices()
    if not offices:
        offices = parse_offices(page.text())
        env.store.upsert_offices(offices, env.log.fetch_id_for(page))
    return HolidayContext(
        env=env,
        adapter=RbiHolidayAdapter(),
        client=env.page.session_client(HOLIDAYS_URL),
        offices=tuple(offices),
        max_year=_max_year(page),
    )


def _max_year(page: RawPayload) -> int:
    options = extract_select_options(
        page.text(), YEAR_SELECT, source=Source.RBI, dataset=Dataset.HOLIDAYS
    )
    years = [int(value) for value, _ in options if value.isdigit()]
    if not years:
        raise ParseError(Source.RBI, Dataset.HOLIDAYS, "no years in the year dropdown")
    return max(years)


def year_loaded(env: RunEnv, year: int) -> bool:
    offices = env.store.offices()
    loaded = env.store.loaded_years()
    return bool(offices) and all(year in loaded.get(o.slug, frozenset()) for o in offices)


def load_holiday_month(
    ctx: HolidayContext, year: int, month: int
) -> tuple[dict[str, HolidayDiff], RawPayload]:
    """POST one month for all offices and replace every office's rows for that month."""
    [raw] = ctx.adapter.fetch(ctx.client, HolidayQuery(year=year, month=month))
    fetch_id = ctx.env.log.fetch_id_for(raw)
    by_office = _group_month(ctx.adapter.parse(raw), ctx.offices, year, month)
    diffs = {
        office.slug: ctx.env.store.replace_holiday_month(
            office.slug, year, month, by_office.get(office.slug, []), fetch_id
        )
        for office in ctx.offices
    }
    return diffs, raw


def _group_month(
    holidays: Sequence[Holiday], offices: Sequence[Office], year: int, month: int
) -> dict[str, list[Holiday]]:
    known = {o.slug for o in offices}
    grouped: dict[str, list[Holiday]] = {}
    for holiday in holidays:
        if (holiday.date.year, holiday.date.month) != (year, month):
            raise ParseError(
                Source.RBI, Dataset.HOLIDAYS, f"{holiday.date} returned for {year}-{month:02d}"
            )
        if holiday.office_slug not in known:
            raise ParseError(
                Source.RBI, Dataset.HOLIDAYS, f"holiday for unknown office {holiday.office_slug!r}"
            )
        grouped.setdefault(holiday.office_slug, []).append(holiday)
    return grouped


def load_holiday_year(ctx: HolidayContext, year: int) -> Loaded:
    """12 monthly POSTs; the year is marked loaded only when every month succeeded."""
    monthly: list[dict[str, HolidayDiff]] = []
    raw: RawPayload | None = None
    for month in range(1, 13):
        diffs, raw = load_holiday_month(ctx, year, month)
        monthly.append(diffs)
    assert raw is not None
    fetch_id = ctx.env.log.fetch_id_for(raw)
    for office in ctx.offices:
        ctx.env.store.mark_holiday_year_loaded(office.slug, year, fetch_id)
    return Loaded(record_holiday_event(ctx.env, str(year), monthly), raw)


def record_holiday_event(
    env: RunEnv, period: str, monthly: Sequence[Mapping[str, HolidayDiff]]
) -> int:
    """Record ``holidays.updated`` (counts per office) when anything changed; return the total."""
    counts: dict[str, dict[str, int]] = {}
    for diffs in monthly:
        for slug, diff in diffs.items():
            if diff.added or diff.removed:
                entry = counts.setdefault(slug, {"added": 0, "removed": 0})
                entry["added"] += len(diff.added)
                entry["removed"] += len(diff.removed)
    if counts:
        env.store.record_event("holidays.updated", {"period": period, "offices": counts})
    return sum(c["added"] + c["removed"] for c in counts.values())


# ---------------------------------------------------------------- fx and mibor
def load_fx(env: RunEnv, adapter: FxAdapter, ranges: Sequence[DateRangeQuery]) -> TaskOutput:
    rows = 0
    last: RawPayload | None = None
    for window in _windows(ranges):
        for raw in adapter.fetch(env.client, window):
            changed = env.store.upsert_fx_rates(adapter.parse(raw), env.log.fetch_id_for(raw))
            record_fx_event(env, changed)
            rows += len(changed)
            last = raw
    if last is None:
        return TaskOutput(rows=0, skipped=True)
    return TaskOutput(rows=rows, fingerprint=adapter.fingerprint(last))


def record_fx_event(env: RunEnv, changed: Sequence[FxRate]) -> None:
    """Record ``fx.rates.published`` as ``{currency: [dates]}`` for recent new or changed rows."""
    cutoff = env.today - dt.timedelta(days=EVENT_RECENCY_DAYS)
    dates: dict[str, list[str]] = {}
    for rate in sorted(changed, key=lambda r: (r.currency.value, r.date)):
        if rate.date >= cutoff:
            dates.setdefault(rate.currency.value, []).append(rate.date.isoformat())
    if dates:
        env.store.record_event("fx.rates.published", dict(dates))


def load_mibor(env: RunEnv, adapter: MiborAdapter, ranges: Sequence[DateRangeQuery]) -> TaskOutput:
    rows = 0
    last: RawPayload | None = None
    for window in _windows(ranges):
        for raw in adapter.fetch(env.client, window):
            rows += env.store.upsert_mibor(adapter.parse(raw), env.log.fetch_id_for(raw))
            last = raw
    if last is None:
        return TaskOutput(rows=0, skipped=True)
    return TaskOutput(rows=rows, fingerprint=adapter.fingerprint(last))


def latest_fx(env: RunEnv, source: Source) -> dt.date | None:
    """Newest stored FX date for ``source`` across all currencies."""
    dates = [env.store.latest_fx_date(currency, source) for currency in Currency]
    return max((d for d in dates if d is not None), default=None)
