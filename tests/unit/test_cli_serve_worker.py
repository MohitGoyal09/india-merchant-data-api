"""CLI: serve, canary, webhooks dispatch, worker (loop driven by a fake clock)."""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import imda.cli as cli
from imda.config import Settings
from imda.events.webhooks import DispatchReport
from imda.health.canary import CanaryReport, CanaryResult
from imda.ingest.common import ExchangeLog, RunSummary
from imda.models import Dataset, Source, SourceStatus
from imda.store.repo import Store
from imda.worker import STEP_SECONDS, ScheduledJob, run_worker

runner = CliRunner()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cli.sqlite3"
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None, db_path=path))

    @contextmanager
    def fake_client(settings: Settings, log: ExchangeLog) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(cli, "_open_client", fake_client)
    return path


# ------------------------------------------------------------------ serve
def test_serve_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: calls.append((a, k)))

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            ("imda.api.app:create_app",),
            {"factory": True, "host": "127.0.0.1", "port": 8000, "reload": False},
        )
    ]


def test_serve_passes_options_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: calls.append(k))

    result = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0", "--port", "8766", "--reload"])

    assert result.exit_code == 0, result.output
    assert calls == [{"factory": True, "host": "0.0.0.0", "port": 8766, "reload": True}]


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_serve_rejects_bad_port(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("must not start"))
    assert runner.invoke(cli.app, ["serve", "--port", port]).exit_code == 2


def test_serve_factory_path_resolves_to_a_real_app() -> None:
    from imda.api.app import create_app

    module, _, name = cli.API_FACTORY.partition(":")
    assert module == "imda.api.app"
    assert name == create_app.__name__


# ------------------------------------------------------------------ canary
def canary_report(*statuses: SourceStatus, error: str | None = None) -> CanaryReport:
    pairs = [
        (Source.RBI, Dataset.HOLIDAYS),
        (Source.RBI, Dataset.FX),
        (Source.FBIL, Dataset.FX),
        (Source.FBIL, Dataset.MIBOR),
    ]
    results = tuple(
        CanaryResult(
            s, d, status, error=error if status is not SourceStatus.OK else None, requests=2
        )
        for (s, d), status in zip(pairs, statuses, strict=False)
    )
    return CanaryReport(run_id="run_c", results=results, requests=2 * len(results))


@pytest.mark.parametrize(
    ("statuses", "code", "overall"),
    [
        ((SourceStatus.OK, SourceStatus.OK), 0, "ok"),
        ((SourceStatus.OK, SourceStatus.DEGRADED), 0, "degraded"),
        ((SourceStatus.DEGRADED, SourceStatus.BROKEN), 1, "broken"),
    ],
)
def test_canary_exit_code_follows_overall_status(
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
    statuses: tuple[SourceStatus, ...],
    code: int,
    overall: str,
) -> None:
    seen: dict[str, Any] = {}

    def fake(store: Store, client: object, **kwargs: Any) -> CanaryReport:
        seen.update(kwargs, client=client)
        return canary_report(*statuses, error="sample did not parse")

    monkeypatch.setattr(cli, "run_canary", fake)

    result = runner.invoke(cli.app, ["canary"])

    assert result.exit_code == code, result.output
    assert f"overall: {overall}" in result.output
    assert "rbi/holidays" in result.output
    assert isinstance(seen["exchange_log"], ExchangeLog)  # exchanges are logged
    assert seen["today"] == cli._today()


def test_canary_table_shows_error_detail(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "run_canary",
        lambda *a, **k: canary_report(SourceStatus.OK, SourceStatus.BROKEN, error="boom"),
    )

    out = runner.invoke(cli.app, ["canary"]).output

    assert "SOURCE/DATASET" in out
    assert "broken" in out
    assert "boom" in out


# ------------------------------------------------------------------ webhooks dispatch
def test_webhooks_dispatch_prints_the_report(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "dispatch_pending", lambda conn, settings, **_: DispatchReport(4, 3, 1, 2)
    )

    result = runner.invoke(cli.app, ["webhooks", "dispatch"])

    assert result.exit_code == 0, result.output
    assert "sent=4 succeeded=3 failed=1 skipped_unsafe=2" in result.output


def test_webhooks_dispatch_with_nothing_due_reports_zeros(db: Path) -> None:
    result = runner.invoke(cli.app, ["webhooks", "dispatch"])

    assert result.exit_code == 0, result.output
    assert "sent=0 succeeded=0 failed=0 skipped_unsafe=0" in result.output


# ------------------------------------------------------------------ worker command
def records(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


@pytest.fixture
def jobs_called(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def refresh(settings: Settings) -> RunSummary:
        calls.append("refresh")
        return RunSummary(run_id="run_w", status="ok", requests=3)

    def dispatch(settings: Settings) -> DispatchReport:
        calls.append("dispatch")
        return DispatchReport(sent=1, succeeded=1)

    monkeypatch.setattr(cli, "_run_refresh", refresh)
    monkeypatch.setattr(cli, "_run_dispatch", dispatch)
    return calls


def test_worker_once_runs_refresh_then_dispatch_and_exits(db: Path, jobs_called: list[str]) -> None:
    result = runner.invoke(cli.app, ["worker", "--once"])

    assert result.exit_code == 0, result.output
    assert jobs_called == ["refresh", "dispatch"]
    cycle = next(r for r in records(result.output) if r["event"] == "worker.cycle")
    assert cycle["refresh"]["status"] == "ok"
    assert cycle["refresh"]["run_id"] == "run_w"
    assert cycle["dispatch"]["sent"] == 1
    assert "ts" in cycle


def test_worker_once_exits_1_when_a_job_fails(
    db: Path, monkeypatch: pytest.MonkeyPatch, jobs_called: list[str]
) -> None:
    monkeypatch.setattr(
        cli, "_run_refresh", lambda s: RunSummary(run_id="run_f", status="failed", requests=1)
    )

    result = runner.invoke(cli.app, ["worker", "--once"])

    assert result.exit_code == 1
    assert jobs_called == ["dispatch"]  # dispatch still ran
    cycle = next(r for r in records(result.output) if r["event"] == "worker.cycle")
    assert cycle["refresh"]["status"] == "failed"


def test_worker_once_survives_a_crashing_job(
    db: Path, monkeypatch: pytest.MonkeyPatch, jobs_called: list[str]
) -> None:
    def crash(settings: Settings) -> RunSummary:
        raise RuntimeError("disk full")

    monkeypatch.setattr(cli, "_run_refresh", crash)

    result = runner.invoke(cli.app, ["worker", "--once"])

    assert result.exit_code == 1
    cycle = next(r for r in records(result.output) if r["event"] == "worker.cycle")
    assert cycle["refresh"]["status"] == "error"
    assert cycle["refresh"]["error"] == "RuntimeError: disk full"
    assert cycle["dispatch"]["status"] == "ok"


@pytest.mark.parametrize("option", ["--refresh-every-minutes", "--dispatch-every-seconds"])
def test_worker_rejects_zero_interval(db: Path, option: str) -> None:
    assert runner.invoke(cli.app, ["worker", option, "0"]).exit_code == 2


def test_worker_command_restores_the_sigterm_handler(db: Path, jobs_called: list[str]) -> None:
    before = signal.getsignal(signal.SIGTERM)

    runner.invoke(cli.app, ["worker", "--once"])

    assert signal.getsignal(signal.SIGTERM) is before


def test_sigterm_handler_sets_the_stop_flag() -> None:
    stop = threading.Event()
    before = signal.getsignal(signal.SIGTERM)

    with cli._sigterm_stops(stop):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert stop.is_set()

    assert signal.getsignal(signal.SIGTERM) is before


# ------------------------------------------------------------------ run_worker (fake clock)
class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_jobs(
    log: list[tuple[str, float]], clock: FakeTime, *, refresh_every: float, dispatch_every: float
) -> list[ScheduledJob]:
    def job(name: str) -> Callable[[], tuple[bool, Mapping[str, object]]]:
        def run() -> tuple[bool, Mapping[str, object]]:
            log.append((name, clock.now))
            return True, {}

        return run

    return [
        ScheduledJob("refresh", refresh_every, job("refresh")),
        ScheduledJob("dispatch", dispatch_every, job("dispatch")),
    ]


def test_loop_runs_jobs_when_due_and_sleeps_in_small_steps() -> None:
    fake, log, out = FakeTime(), [], []
    jobs = make_jobs(log, fake, refresh_every=60, dispatch_every=20)

    run_worker(
        jobs,
        emit=out.append,
        clock=fake.clock,
        sleep=fake.sleep,
        should_stop=lambda: fake.now >= 1000 + 125,
    )

    assert [(n, t - 1000) for n, t in log if n == "refresh"] == [
        ("refresh", 0),
        ("refresh", 60),
        ("refresh", 120),
    ]
    assert [t - 1000 for n, t in log if n == "dispatch"] == [0, 20, 40, 60, 80, 100, 120]
    assert max(fake.sleeps) <= STEP_SECONDS
    assert out[0]["event"] == "worker.start"
    assert out[-1] == {"event": "worker.stop"}
    cycles = [r for r in out if r["event"] == "worker.cycle"]
    assert "refresh" in cycles[0]
    assert "dispatch" in cycles[0]  # both due at start
    assert "refresh" not in cycles[1]  # one record per cycle, only jobs that ran


def test_loop_does_not_sleep_past_the_next_due_time() -> None:
    fake, log = FakeTime(), []
    jobs = make_jobs(log, fake, refresh_every=8, dispatch_every=1000)

    run_worker(
        jobs,
        emit=lambda r: None,
        clock=fake.clock,
        sleep=fake.sleep,
        should_stop=lambda: fake.now >= 1010,
    )

    assert fake.sleeps[:2] == [STEP_SECONDS, 3.0]  # capped step, then exactly to the due time


def test_once_runs_every_job_once_without_sleeping() -> None:
    fake, log = FakeTime(), []

    ok = run_worker(
        make_jobs(log, fake, refresh_every=60, dispatch_every=20),
        emit=lambda r: None,
        once=True,
        clock=fake.clock,
        sleep=fake.sleep,
    )

    assert ok is True
    assert [n for n, _ in log] == ["refresh", "dispatch"]
    assert fake.sleeps == []


def test_a_failing_job_is_retried_at_its_next_slot_and_does_not_stop_the_loop() -> None:
    fake, out = FakeTime(), []
    attempts: list[float] = []

    def flaky() -> tuple[bool, Mapping[str, object]]:
        attempts.append(fake.now)
        raise ValueError("upstream down")

    run_worker(
        [ScheduledJob("refresh", 10, flaky)],
        emit=out.append,
        clock=fake.clock,
        sleep=fake.sleep,
        should_stop=lambda: fake.now >= 1025,
    )

    assert [t - 1000 for t in attempts] == [0, 10, 20]
    errors = [r["refresh"] for r in out if r["event"] == "worker.cycle"]
    assert {e["status"] for e in errors} == {"error"}
    assert errors[0]["error"] == "ValueError: upstream down"


def test_keyboard_interrupt_stops_gracefully() -> None:
    fake, out = FakeTime(), []

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    ok = run_worker(
        make_jobs([], fake, refresh_every=60, dispatch_every=20),
        emit=out.append,
        clock=fake.clock,
        sleep=interrupt,
    )

    assert ok is True
    assert out[-1] == {"event": "worker.stop"}


def test_stop_flag_checked_before_any_work() -> None:
    log: list[tuple[str, float]] = []
    fake = FakeTime()

    run_worker(
        make_jobs(log, fake, refresh_every=1, dispatch_every=1),
        emit=lambda r: None,
        clock=fake.clock,
        sleep=fake.sleep,
        should_stop=lambda: True,
    )

    assert log == []
