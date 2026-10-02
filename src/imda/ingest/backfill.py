"""Backfill: load historical data for a date range. Idempotent; safe to re-run."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from imda.health.drift import Baselines
from imda.ingest.common import ExchangeLog, RunEnv, RunSummary, Task, TaskOutput, run_plan
from imda.ingest.loaders import (
    FBIL_FX_START,
    FBIL_MIBOR_START,
    HOLIDAYS_FIRST_YEAR,
    floored_range,
    load_fx,
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


def backfill(
    store: Store,
    client: HttpClient,
    *,
    start: dt.date,
    end: dt.date,
    datasets: set[Dataset],
    force: bool = False,
    today: dt.date,
    exchange_log: ExchangeLog | None = None,
    baselines: Baselines | None = None,
) -> RunSummary:
    """Load ``datasets`` for ``[start, end]`` (``end`` is capped at ``today``).

    Each (source, dataset) runs in isolation: one failing never stops the others.
    ``force`` re-fetches holiday years that are already marked loaded.
    """
    if not datasets:
        raise ValueError("no datasets selected")
    end = min(end, today)
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    plan = _plan(start, end, datasets, force)
    return run_plan(
        store, client, "backfill", plan, today=today, exchange_log=exchange_log, baselines=baselines
    )


def _plan(start: dt.date, end: dt.date, datasets: set[Dataset], force: bool) -> list[Task]:
    plan: list[Task] = []
    if Dataset.OFFICES in datasets:
        plan.append((Source.RBI, Dataset.OFFICES, load_offices))
    if Dataset.HOLIDAYS in datasets:
        plan.append((Source.RBI, Dataset.HOLIDAYS, _holidays_task(start, end, force)))
    if Dataset.FX in datasets:
        plan.append((Source.RBI, Dataset.FX, _rbi_fx(start, end)))
        plan.append((Source.FBIL, Dataset.FX, _fbil_fx(start, end)))
    if Dataset.MIBOR in datasets:
        plan.append((Source.FBIL, Dataset.MIBOR, _fbil_mibor(start, end)))
    return plan


def _holidays_task(start: dt.date, end: dt.date, force: bool) -> Callable[[RunEnv], TaskOutput]:
    def run(env: RunEnv) -> TaskOutput:
        years = [
            y
            for y in range(max(start.year, HOLIDAYS_FIRST_YEAR), end.year + 1)
            if force or not year_loaded(env, y)
        ]
        if not years:
            return TaskOutput(rows=0, skipped=True)
        ctx = prepare_holidays(env)
        rows, last = 0, None
        for year in (y for y in years if y <= ctx.max_year):
            loaded = load_holiday_year(ctx, year)
            rows, last = rows + loaded.rows, loaded.raw
        if last is None:
            return TaskOutput(rows=0, skipped=True)
        return TaskOutput(rows=rows, fingerprint=ctx.adapter.fingerprint(last))

    return run


def _rbi_fx(start: dt.date, end: dt.date) -> Callable[[RunEnv], TaskOutput]:
    return lambda env: load_fx(env, RbiFxAdapter(), rbi_fx_ranges(start, end))


def _fbil_fx(start: dt.date, end: dt.date) -> Callable[[RunEnv], TaskOutput]:
    return lambda env: load_fx(env, FbilFxAdapter(), floored_range(start, end, FBIL_FX_START))


def _fbil_mibor(start: dt.date, end: dt.date) -> Callable[[RunEnv], TaskOutput]:
    return lambda env: load_mibor(
        env, FbilMiborAdapter(), floored_range(start, end, FBIL_MIBOR_START)
    )
