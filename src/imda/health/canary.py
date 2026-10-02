"""Drift canary: one small sample per dataset, compared with the recorded baselines.

The canary fetches (at most ``MAX_REQUESTS`` requests), parses, fingerprints and compares. It
writes only source health, the run, the fetch log and ``source.degraded`` / ``source.recovered``
events. It never writes rates, holidays or offices, so it is safe to run at any time.

Outcome per dataset: ``ok``; ``degraded`` when the shape drifted or the upstream failed
(parsing worked, or we could not reach it); ``broken`` when the sample no longer parses.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from imda.health.drift import Baselines, DriftReport
from imda.ingest.common import (
    ExchangeLog,
    RunEnv,
    RunSummary,
    Task,
    TaskOutput,
    TaskResult,
    run_plan,
)
from imda.ingest.loaders import (
    FBIL_FX_START,
    FBIL_MIBOR_START,
    FxAdapter,
    MiborAdapter,
    floored_range,
    rbi_fx_ranges,
)
from imda.models import Dataset, Source, SourceStatus
from imda.sources.base import DateRangeQuery, HolidayQuery, HttpClient, ParseError, RawPayload
from imda.sources.fbil.fx import FbilFxAdapter
from imda.sources.fbil.mibor import FbilMiborAdapter
from imda.sources.rbi.fx import RbiFxAdapter
from imda.sources.rbi.holidays import HOLIDAYS_URL, RbiHolidayAdapter
from imda.sources.rbi.offices import parse_offices
from imda.store.repo import Store

SAMPLE_DAYS = 7
MAX_REQUESTS = 6
"""Holidays: page GET + month POST. RBI FX: form GET + POST. FBIL FX and MIBOR: one GET each."""
_SEVERITY = {
    SourceStatus.UNKNOWN: 0,
    SourceStatus.OK: 1,
    SourceStatus.DEGRADED: 2,
    SourceStatus.BROKEN: 3,
}


@dataclass(frozen=True, slots=True)
class CanaryResult:
    source: Source
    dataset: Dataset
    status: SourceStatus
    drift: DriftReport | None = None
    error: str | None = None
    requests: int = 0

    @property
    def key(self) -> str:
        return f"{self.source.value}/{self.dataset.value}"

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"status": self.status.value, "requests": self.requests}
        if self.drift is not None:
            data["drift"] = self.drift.as_dict()
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass(frozen=True, slots=True)
class CanaryReport:
    run_id: str
    results: tuple[CanaryResult, ...]
    requests: int

    @property
    def status(self) -> SourceStatus:
        """The worst status of any dataset."""
        return max(
            (r.status for r in self.results),
            key=_SEVERITY.__getitem__,
            default=SourceStatus.UNKNOWN,
        )

    @property
    def healthy(self) -> bool:
        return self.status is SourceStatus.OK

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "requests": self.requests,
            "results": {r.key: r.as_dict() for r in self.results},
        }


def run_canary(
    store: Store,
    client: HttpClient,
    *,
    today: dt.date,
    exchange_log: ExchangeLog | None = None,
    baselines: Baselines | None = None,
) -> CanaryReport:
    """Sample every dataset once and record the outcome as source health."""
    plan: list[Task] = [
        (Source.RBI, Dataset.HOLIDAYS, _holidays_sample),
        (Source.RBI, Dataset.FX, _rbi_fx_sample),
        (Source.FBIL, Dataset.FX, _fbil_fx_sample),
        (Source.FBIL, Dataset.MIBOR, _fbil_mibor_sample),
    ]
    summary = run_plan(
        store, client, "canary", plan, today=today, exchange_log=exchange_log, baselines=baselines
    )
    return _report(summary)


def _report(summary: RunSummary) -> CanaryReport:
    return CanaryReport(
        run_id=summary.run_id,
        results=tuple(_result(task) for task in summary.tasks),
        requests=summary.requests,
    )


def _result(task: TaskResult) -> CanaryResult:
    return CanaryResult(
        source=task.source,
        dataset=task.dataset,
        status=task.health or SourceStatus.UNKNOWN,
        drift=task.drift,
        error=task.error if task.error is not None else _drift_error(task.drift),
        requests=task.requests,
    )


def _drift_error(drift: DriftReport | None) -> str | None:
    return drift.summary() if drift is not None and drift.drifted else None


# ---------------------------------------------------------------- samples
def _holidays_sample(env: RunEnv) -> TaskOutput:
    """The current month for all offices. The page GET also proves the office dropdown parses."""
    page = env.page.raw(HOLIDAYS_URL)
    env.log.fetch_id_for(page)
    if not parse_offices(page.text()):
        raise ParseError(Source.RBI, Dataset.HOLIDAYS, "no offices in the dropdown")
    adapter = RbiHolidayAdapter()
    [raw] = adapter.fetch(
        env.page.session_client(HOLIDAYS_URL),
        HolidayQuery(year=env.today.year, month=env.today.month),
    )
    env.log.fetch_id_for(raw)
    holidays = adapter.parse(raw)
    return TaskOutput(rows=len(holidays), fingerprint=adapter.fingerprint(raw))


def _rbi_fx_sample(env: RunEnv) -> TaskOutput:
    return _sample(env, RbiFxAdapter(), rbi_fx_ranges(_week_start(env.today), env.today))


def _fbil_fx_sample(env: RunEnv) -> TaskOutput:
    ranges = floored_range(_week_start(env.today), env.today, FBIL_FX_START)
    return _sample(env, FbilFxAdapter(), ranges)


def _fbil_mibor_sample(env: RunEnv) -> TaskOutput:
    ranges = floored_range(_week_start(env.today), env.today, FBIL_MIBOR_START)
    return _sample(env, FbilMiborAdapter(), ranges)


def _week_start(today: dt.date) -> dt.date:
    return today - dt.timedelta(days=SAMPLE_DAYS - 1)


def _sample(
    env: RunEnv, adapter: FxAdapter | MiborAdapter, ranges: list[DateRangeQuery]
) -> TaskOutput:
    """Fetch, parse and fingerprint without storing.

    A week with no rows still parsed, so it is not a broken source: FBIL can be days behind (see
    ``freshness``). Drift is only judged when the sample has rows to take a shape from.
    """
    rows = 0
    last: RawPayload | None = None
    for window in ranges:
        for raw in adapter.fetch(env.client, window):
            env.log.fetch_id_for(raw)
            rows += len(adapter.parse(raw))
            last = raw
    if last is None:
        return TaskOutput(rows=0, skipped=True)
    return TaskOutput(rows=rows, fingerprint=adapter.fingerprint(last))
