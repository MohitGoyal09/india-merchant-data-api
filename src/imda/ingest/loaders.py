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
from imda.store.repo import HolidayDiff, Store

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
    years: tuple[int, ...]
    """Every year in RBI's ``drYear`` dropdown, ascending."""

    @property
    def max_year(self) -> int:
        return self.years[-1]


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
        years=offered_years(page),
    )


def offered_years(page: RawPayload) -> tuple[int, ...]:
    """The years in RBI's ``drYear`` dropdown, ascending."""
    options = extract_select_options(
        page.text(), YEAR_SELECT, source=Source.RBI, dataset=Dataset.HOLIDAYS
    )
    years = sorted({int(value) for value, _ in options if value.isdigit()})
    if not years:
        raise ParseError(Source.RBI, Dataset.HOLIDAYS, "no years in the year dropdown")
    return tuple(years)


def year_loaded(env: RunEnv, year: int) -> bool:
    return year in fully_loaded_years(env.store)


def fully_loaded_years(store: Store) -> frozenset[int]:
    """Years whose 12 months are loaded for every office (none while there are no offices)."""
    offices = store.offices()
    if not offices:
        return frozenset()
    loaded = store.loaded_years()
    per_office = [loaded.get(o.slug, frozenset()) for o in offices]
    return frozenset.intersection(*per_office)


def load_holiday_month(
    ctx: HolidayContext, year: int, month: int
) -> tuple[dict[str, HolidayDiff], RawPayload]:
    """POST one month for all offices and replace every office's rows in one transaction.

    The store records ``holidays.updated`` in that same transaction.
    """
    [raw] = ctx.adapter.fetch(ctx.client, HolidayQuery(year=year, month=month))
    fetch_id = ctx.env.log.fetch_id_for(raw)
    by_office = _group_month(ctx.adapter.parse(raw), ctx.offices, year, month)
    diffs = ctx.env.store.replace_holiday_month_all(
        year, month, {o.slug: by_office.get(o.slug, []) for o in ctx.offices}, fetch_id
    )
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
    return Loaded(count_changes(monthly), raw)


def count_changes(monthly: Sequence[Mapping[str, HolidayDiff]]) -> int:
    """Total holidays added plus removed across the months' per-office diffs."""
    return sum(len(d.added) + len(d.removed) for diffs in monthly for d in diffs.values())


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
