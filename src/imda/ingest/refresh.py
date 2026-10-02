"""Refresh: pull what is new since the last run. Cheap enough to run daily."""

from __future__ import annotations

import datetime as dt

from imda.health.drift import Baselines
from imda.ingest.common import ExchangeLog, RunEnv, RunSummary, Task, TaskOutput, run_plan
from imda.ingest.loaders import (
    FBIL_FX_START,
    FBIL_MIBOR_START,
    HOLIDAYS_FIRST_YEAR,
    HolidayContext,
    count_changes,
    floored_range,
    latest_fx,
    load_fx,
    load_holiday_month,
    load_holiday_year,
    load_mibor,
    load_offices,
    prepare_holidays,
    rbi_fx_ranges,
    year_loaded,
)
from imda.models import Dataset, Source
from imda.sources.base import HttpClient
from imda.sources.fbil.fx import FbilFxAdapter
from imda.sources.fbil.mibor import FbilMiborAdapter
from imda.sources.rbi.fx import RbiFxAdapter
from imda.store.repo import Store

OVERLAP_DAYS = 7
"""Re-fetch this far back from the newest stored date, to pick up revisions."""
EMPTY_LOOKBACK_DAYS = 30
"""How far back to start when a source has no rows yet."""


def refresh(
    store: Store,
    client: HttpClient,
    *,
    today: dt.date,
    exchange_log: ExchangeLog | None = None,
    baselines: Baselines | None = None,
) -> RunSummary:
    """Offices, holidays (this month, next month, this year if missing), FX and MIBOR."""
    plan: list[Task] = [
        (Source.RBI, Dataset.OFFICES, load_offices),
        (Source.RBI, Dataset.HOLIDAYS, _refresh_holidays),
        (Source.RBI, Dataset.FX, _rbi_fx),
        (Source.FBIL, Dataset.FX, _fbil_fx),
        (Source.FBIL, Dataset.MIBOR, _fbil_mibor),
    ]
    return run_plan(
        store, client, "refresh", plan, today=today, exchange_log=exchange_log, baselines=baselines
    )


def _since(latest: dt.date | None, today: dt.date) -> dt.date:
    if latest is None:
        return today - dt.timedelta(days=EMPTY_LOOKBACK_DAYS)
    return latest - dt.timedelta(days=OVERLAP_DAYS)


def _rbi_fx(env: RunEnv) -> TaskOutput:
    start = _since(latest_fx(env, Source.RBI), env.today)
    return load_fx(env, RbiFxAdapter(), rbi_fx_ranges(start, env.today))


def _fbil_fx(env: RunEnv) -> TaskOutput:
    start = _since(latest_fx(env, Source.FBIL), env.today)
    return load_fx(env, FbilFxAdapter(), floored_range(start, env.today, FBIL_FX_START))


def _fbil_mibor(env: RunEnv) -> TaskOutput:
    start = _since(env.store.latest_mibor_date(), env.today)
    return load_mibor(env, FbilMiborAdapter(), floored_range(start, env.today, FBIL_MIBOR_START))


def _refresh_holidays(env: RunEnv) -> TaskOutput:
    ctx = prepare_holidays(env)
    months = _months_to_load(env, ctx)
    rows, last = 0, None
    if not year_loaded(env, env.today.year) and _year_in_range(env.today.year, ctx):
        loaded = load_holiday_year(ctx, env.today.year)
        rows, last = loaded.rows, loaded.raw
        months = [m for m in months if m[0] != env.today.year]
    for year, month in months:
        diffs, last = load_holiday_month(ctx, year, month)
        rows += count_changes([diffs])
    if last is None:
        return TaskOutput(rows=0, skipped=True)
    return TaskOutput(rows=rows, fingerprint=ctx.adapter.fingerprint(last))


def _months_to_load(env: RunEnv, ctx: HolidayContext) -> list[tuple[int, int]]:
    this = env.today.replace(day=1)
    following = (this + dt.timedelta(days=32)).replace(day=1)
    return [(d.year, d.month) for d in (this, following) if _year_in_range(d.year, ctx)]


def _year_in_range(year: int, ctx: HolidayContext) -> bool:
    return HOLIDAYS_FIRST_YEAR <= year <= ctx.max_year
