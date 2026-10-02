"""Shared ingest machinery: exchange logging, task isolation, health transitions, run summary.

The only mutable state in ingest lives in ``ExchangeLog`` (which run/source/dataset the next
upstream attempt belongs to) and ``HolidayPage`` (the one holiday-page GET per run).
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from imda.health.drift import Baselines, DriftReport, check_drift, default_baselines
from imda.http.client import ExchangeEvent
from imda.models import Dataset, Source, SourceStatus
from imda.sources.base import HttpClient, ParseError, RawPayload, UpstreamError, UpstreamRequest
from imda.store.repo import RunKind, Store, exchange_params

logger = logging.getLogger(__name__)
MAX_ERROR_CHARS = 500

TaskStatus = Literal["ok", "failed", "skipped"]
_UNHEALTHY = (SourceStatus.DEGRADED, SourceStatus.BROKEN)
_NO_HISTORY = (None, SourceStatus.UNKNOWN, SourceStatus.OK)
_FetchKey = tuple[str, str, str, str]


class ExchangeLog:
    """Logs every upstream attempt to ``fetch_log`` under the current run/source/dataset.

    Pass it as ``PoliteClient(on_exchange=log)``. ``fetch_id_for`` maps a received payload
    back to its logged attempt; when the client did not log it (a simple fake), the payload
    is logged on the spot so every stored row still has provenance.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._context: tuple[str, Source, Dataset] | None = None
        self._ids: dict[_FetchKey, str] = {}
        self.attempts = 0

    def bind(self, run_id: str, source: Source, dataset: Dataset) -> None:
        self._context = (run_id, source, dataset)

    def __call__(self, event: ExchangeEvent) -> None:
        fetch_id = self._log(event)
        if event.error is None and event.sha256 is not None:
            self._ids[_fetch_key(event.request, event.sha256)] = fetch_id

    def fetch_id_for(self, raw: RawPayload) -> str:
        known = self._ids.get(_fetch_key(raw.request, raw.sha256))
        if known is not None:
            return known
        event = ExchangeEvent(
            request=raw.request,
            attempt=1,
            status_code=raw.status_code,
            bytes=len(raw.body),
            sha256=raw.sha256,
            duration_ms=raw.duration_ms,
            fetched_at=raw.fetched_at,
            error=None,
        )
        self(event)
        return self._ids[_fetch_key(raw.request, raw.sha256)]

    def _log(self, event: ExchangeEvent) -> str:
        if self._context is None:
            raise RuntimeError("upstream exchange outside of an ingest run")
        run_id, source, dataset = self._context
        self.attempts += 1
        return self._store.log_exchange(run_id, source, dataset, event)


def _fetch_key(request: UpstreamRequest, sha256: str) -> _FetchKey:
    params = sorted(exchange_params(request).items())
    return request.method, request.url, repr(params), sha256


@dataclass(frozen=True, slots=True)
class TaskOutput:
    rows: int
    fingerprint: dict[str, object] | None = None
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class TaskResult:
    source: Source
    dataset: Dataset
    status: TaskStatus
    rows: int = 0
    requests: int = 0
    error: str | None = None
    health: SourceStatus | None = None
    """Health recorded for the source (``degraded`` when parsing worked but the shape drifted)."""
    drift: DriftReport | None = None

    @property
    def key(self) -> str:
        return f"{self.source.value}/{self.dataset.value}"


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    status: Literal["ok", "partial", "failed"]
    tasks: tuple[TaskResult, ...] = field(default_factory=tuple)
    requests: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "tasks": {t.key: _task_dict(t) for t in self.tasks},
        }


def _task_dict(task: TaskResult) -> dict[str, object]:
    entry: dict[str, object] = {
        "status": task.status,
        "rows": task.rows,
        "requests": task.requests,
        "error": task.error,
    }
    if task.drift is not None and task.drift.drifted:
        entry["drift"] = task.drift.as_dict()
    return entry


@dataclass(frozen=True, slots=True)
class RunEnv:
    store: Store
    client: HttpClient
    log: ExchangeLog
    run_id: str
    today: dt.date
    page: HolidayPage
    baselines: Baselines


Task = tuple[Source, Dataset, Callable[[RunEnv], TaskOutput]]


def run_plan(
    store: Store,
    client: HttpClient,
    kind: RunKind,
    plan: Sequence[Task],
    *,
    today: dt.date,
    exchange_log: ExchangeLog | None = None,
    baselines: Baselines | None = None,
) -> RunSummary:
    """Run each task in isolation inside one ``ingest_runs`` row.

    ``baselines`` default to the packaged ones; a fingerprint that drifts from its baseline
    marks the source ``degraded`` (the data just parsed is still kept).
    """
    log = exchange_log if exchange_log is not None else ExchangeLog(store)
    run_id = store.start_run(kind)
    env = RunEnv(
        store=store,
        client=client,
        log=log,
        run_id=run_id,
        today=today,
        page=HolidayPage(client),
        baselines=default_baselines() if baselines is None else baselines,
    )
    start_attempts = log.attempts
    try:
        results = tuple(execute_task(env, source, dataset, fn) for source, dataset, fn in plan)
    except BaseException as exc:
        store.finish_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    summary = RunSummary(
        run_id=run_id,
        status=_overall(results),
        tasks=results,
        requests=log.attempts - start_attempts,
    )
    store.finish_run(run_id, summary.status, summary.as_dict())
    return summary


def _overall(results: Sequence[TaskResult]) -> Literal["ok", "partial", "failed"]:
    ran = [r for r in results if r.status != "skipped"]
    failed = sum(1 for r in ran if r.status == "failed")
    if failed == 0:
        return "ok"
    return "failed" if failed == len(ran) else "partial"


def execute_task(
    env: RunEnv, source: Source, dataset: Dataset, fn: Callable[[RunEnv], TaskOutput]
) -> TaskResult:
    """Run one (source, dataset) task; any failure is recorded as health, never raised."""
    env.log.bind(env.run_id, source, dataset)
    before = env.log.attempts
    try:
        output = fn(env)
    except UpstreamError as exc:
        return _failed(env, source, dataset, SourceStatus.DEGRADED, str(exc), before)
    except (ParseError, ValueError, sqlite3.IntegrityError) as exc:
        return _failed(env, source, dataset, SourceStatus.BROKEN, _describe(exc), before)
    except Exception as exc:
        logger.exception("unexpected error in %s/%s", source.value, dataset.value)
        message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        return _failed(env, source, dataset, SourceStatus.BROKEN, message, before)
    requests = env.log.attempts - before
    if output.skipped:
        return TaskResult(source, dataset, "skipped", requests=requests)
    drift = _check_drift(env, source, dataset, output.fingerprint)
    status = SourceStatus.DEGRADED if drift is not None and drift.drifted else SourceStatus.OK
    error = drift.summary() if status is SourceStatus.DEGRADED and drift is not None else None
    previous = env.store.set_source_health(
        source,
        dataset,
        status,
        error=error,
        fingerprint=output.fingerprint,
        drift=None if drift is None else drift.as_dict(),
    )
    _record_transition(env.store, source, dataset, previous, status, error)
    return TaskResult(
        source, dataset, "ok", rows=output.rows, requests=requests, health=status, drift=drift
    )


def _check_drift(
    env: RunEnv, source: Source, dataset: Dataset, fingerprint: dict[str, object] | None
) -> DriftReport | None:
    if fingerprint is None:
        return None
    return check_drift(env.baselines, source, dataset, fingerprint)


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, ParseError) else f"{type(exc).__name__}: {exc}"


def _failed(
    env: RunEnv,
    source: Source,
    dataset: Dataset,
    status: SourceStatus,
    error: str,
    before: int,
) -> TaskResult:
    previous = env.store.set_source_health(source, dataset, status, error=error)
    _record_transition(env.store, source, dataset, previous, status, error)
    return TaskResult(
        source,
        dataset,
        "failed",
        requests=env.log.attempts - before,
        error=error,
        health=status,
    )


def _record_transition(
    store: Store,
    source: Source,
    dataset: Dataset,
    previous: SourceStatus | None,
    current: SourceStatus,
    error: str | None,
) -> None:
    """Emit ``source.degraded`` / ``source.recovered`` once per transition."""
    payload: dict[str, object] = {
        "source": source.value,
        "dataset": dataset.value,
        "status": current.value,
        "previous": None if previous is None else previous.value,
    }
    if current in _UNHEALTHY and previous in _NO_HISTORY:
        store.record_event("source.degraded", {**payload, "error": error})
    elif current is SourceStatus.OK and previous in _UNHEALTHY:
        store.record_event("source.recovered", payload)


class HolidayPage:
    """The RBI holiday page, fetched at most once per run.

    The offices dropdown, the year dropdown and the holidays postback session all start from
    this one GET. ``session_client`` answers the postback session's first GET from the cached
    copy, so the run spends one request instead of two.
    """

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._raw: RawPayload | None = None

    def raw(self, url: str) -> RawPayload:
        if self._raw is None:
            self._raw = self._client.send(UpstreamRequest(method="GET", url=url))
        return self._raw

    def session_client(self, url: str) -> HttpClient:
        return _PrimedClient(self._client, self.raw(url))


class _PrimedClient:
    """Serves one pre-fetched GET, then delegates. Stale state is re-fetched for real."""

    def __init__(self, inner: HttpClient, primed: RawPayload) -> None:
        self._inner = inner
        self._primed: RawPayload | None = primed

    def send(self, request: UpstreamRequest) -> RawPayload:
        primed = self._primed
        if primed is not None and request == primed.request:
            self._primed = None
            return primed
        return self._inner.send(request)
