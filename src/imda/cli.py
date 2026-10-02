"""Command-line entry point: ``imda <command>``."""

from __future__ import annotations

import datetime as dt
import json
import signal
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict
from enum import StrEnum
from typing import Annotated

import typer
import uvicorn

from imda import __version__
from imda.config import Settings, get_settings
from imda.events.webhooks import DispatchReport, dispatch_pending
from imda.health.canary import CanaryReport, run_canary
from imda.http.client import PoliteClient
from imda.ingest.backfill import backfill
from imda.ingest.common import ExchangeLog, RunSummary
from imda.ingest.refresh import refresh
from imda.models import IST, Dataset, SourceStatus
from imda.sources.base import HttpClient
from imda.store.repo import Store
from imda.worker import JobOutcome, ScheduledJob, run_worker

app = typer.Typer(no_args_is_help=True, add_completion=False)
webhooks_app = typer.Typer(help="Webhook delivery.", no_args_is_help=True)
app.add_typer(webhooks_app, name="webhooks")

DATASET_NAMES: dict[str, Dataset] = {
    "offices": Dataset.OFFICES,
    "holidays": Dataset.HOLIDAYS,
    "fx": Dataset.FX,
    "mibor": Dataset.MIBOR,
}
RECENT_RUNS = 5
DEFAULT_HOST = "127.0.0.1"
"""Loopback only. A container passes ``--host 0.0.0.0`` on purpose."""
API_FACTORY = "imda.api.app:create_app"


@app.callback()
def main() -> None:
    """India Merchant Data API: ingest, serve and check RBI + FBIL data."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


def _open_client(settings: Settings, log: ExchangeLog) -> AbstractContextManager[HttpClient]:
    """The polite upstream client; every attempt is logged through ``log``. Tests replace this."""
    return PoliteClient(settings, on_exchange=log)


def _today() -> dt.date:
    return dt.datetime.now(IST).date()


def _parse_date(option: str, value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{value!r} is not a YYYY-MM-DD date", param_hint=option) from exc


def _parse_datasets(value: str) -> set[Dataset]:
    names = {name.strip().lower() for name in value.split(",") if name.strip()}
    unknown = sorted(names - DATASET_NAMES.keys())
    if unknown or not names:
        raise typer.BadParameter(
            f"unknown dataset(s) {unknown or value!r}; choose from {', '.join(DATASET_NAMES)}",
            param_hint="--datasets",
        )
    return {DATASET_NAMES[name] for name in names}


def _report(summary: RunSummary) -> None:
    typer.echo(f"run {summary.run_id}: {summary.status} ({summary.requests} requests)")
    for task in summary.tasks:
        detail = f"  {task.error}" if task.error else ""
        typer.echo(
            f"  {task.key:<28} {task.status:<8} rows={task.rows:<7} req={task.requests}{detail}"
        )
    if summary.status == "partial":
        typer.echo("warning: some datasets failed; the others were ingested", err=True)
    if summary.status == "failed":
        typer.echo("error: every dataset failed", err=True)
        raise typer.Exit(code=1)


@app.command("backfill")
def backfill_command(
    start: Annotated[str, typer.Option("--from", help="First date, YYYY-MM-DD.")],
    end: Annotated[str | None, typer.Option("--to", help="Last date (default: today).")] = None,
    datasets: Annotated[
        str, typer.Option(help="Comma list of offices,holidays,fx,mibor.")
    ] = ",".join(DATASET_NAMES),
    force: Annotated[bool, typer.Option(help="Re-fetch holiday years already loaded.")] = False,
) -> None:
    """Load historical data into the local database. Safe to re-run."""
    today = _today()
    first = _parse_date("--from", start)
    last = _parse_date("--to", end) if end else today
    selected = _parse_datasets(datasets)
    if first > last:
        raise typer.BadParameter(f"--from {first} is after --to {last}", param_hint="--from")
    settings = get_settings()
    with Store.open(settings.db_path) as store:
        log = ExchangeLog(store)
        with _open_client(settings, log) as client:
            summary = backfill(
                store,
                client,
                start=first,
                end=last,
                datasets=selected,
                force=force,
                today=today,
                exchange_log=log,
            )
    _report(summary)


def _run_refresh(settings: Settings) -> RunSummary:
    with Store.open(settings.db_path) as store:
        log = ExchangeLog(store)
        with _open_client(settings, log) as client:
            return refresh(store, client, today=_today(), exchange_log=log)


def _run_canary(settings: Settings) -> CanaryReport:
    with Store.open(settings.db_path) as store:
        log = ExchangeLog(store)
        with _open_client(settings, log) as client:
            return run_canary(store, client, today=_today(), exchange_log=log)


def _run_dispatch(settings: Settings) -> DispatchReport:
    with Store.open(settings.db_path) as store:
        return dispatch_pending(store.connection, settings)


@app.command("refresh")
def refresh_command() -> None:
    """Fetch what is new: offices, holidays, FX and MIBOR."""
    _report(_run_refresh(get_settings()))


@app.command("status")
def status_command() -> None:
    """Show source health and the most recent ingest runs."""
    settings = get_settings()
    with Store.open(settings.db_path) as store:
        health, runs = store.source_health(), store.runs(RECENT_RUNS)
    typer.echo(f"{'SOURCE/DATASET':<28} {'STATUS':<9} {'LAST OK (UTC)':<19} LAST ERROR")
    for row in health:
        name = f"{row['source']}/{row['dataset']}"
        typer.echo(
            f"{name:<28} {row['status']!s:<9} {str(row['last_success_at'] or '-')[:19]:<19} "
            f"{row['last_error'] or '-'}"
        )
    if not health:
        typer.echo("(no source health recorded yet: run `imda refresh` or `imda backfill`)")
    typer.echo("")
    typer.echo(f"{'RUN':<36} {'KIND':<9} {'STATUS':<8} STARTED")
    for run in runs:
        typer.echo(
            f"{run['run_id']!s:<36} {run['kind']!s:<9} {run['status']!s:<8} {run['started_at']}"
        )


@app.command("serve")
def serve_command(
    host: Annotated[str, typer.Option(help="Bind address.")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on code changes (dev only).")] = False,
) -> None:
    """Run the API with uvicorn."""
    uvicorn.run(API_FACTORY, factory=True, host=host, port=port, reload=reload)


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


MCP_PORT = 8100
MCP_TOOLSETS_HELP = "Comma list of calendar,settlement,fx,rates,health (default: all)."


def _parse_toolsets(value: str | None) -> frozenset[str] | None:
    from imda.mcp.tools import ALL_TOOLSETS  # lazy: keeps other `imda` commands fast

    if value is None:
        return None
    names = frozenset(name.strip().lower() for name in value.split(",") if name.strip())
    unknown = sorted(names - ALL_TOOLSETS)
    if unknown or not names:
        raise typer.BadParameter(
            f"unknown toolset(s) {unknown or value!r}; "
            f"choose from {', '.join(sorted(ALL_TOOLSETS))}",
            param_hint="--toolsets",
        )
    return names


@app.command("mcp")
def mcp_command(
    transport: Annotated[
        McpTransport, typer.Option(help="stdio for Claude Code/Desktop; http for remote hosts.")
    ] = McpTransport.STDIO,
    host: Annotated[str, typer.Option(help="Bind address (http only).")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port (http only).")] = MCP_PORT,
    toolsets: Annotated[str | None, typer.Option(help=MCP_TOOLSETS_HELP)] = None,
) -> None:
    """Run the read-only MCP server (13 tools over the same data as the REST API).

    stdio needs no token. http serves /mcp and needs IMDA_MCP_TOKEN (32+ chars) as Bearer.
    """
    from imda.mcp.server import build_http_app, build_server

    settings = get_settings()
    chosen = _parse_toolsets(toolsets)
    if transport is McpTransport.HTTP:
        secret = settings.mcp_token.get_secret_value() if settings.mcp_token else ""
        if not secret:
            typer.echo(
                "error: HTTP transport needs IMDA_MCP_TOKEN (32+ characters) for Bearer auth",
                err=True,
            )
            raise typer.Exit(code=2)
        server = build_server(settings, toolsets=chosen)
        uvicorn.run(build_http_app(server, secret, host=host), host=host, port=port)
        return
    build_server(settings, toolsets=chosen).run("stdio")


@app.command("canary")
def canary_command() -> None:
    """Sample every source once, compare it with the baselines and record source health.

    Exit code 1 when a source is broken (its sample no longer parses); 0 otherwise.
    """
    report = _run_canary(get_settings())
    typer.echo(f"{'SOURCE/DATASET':<28} {'STATUS':<9} {'REQ':>3}  DETAIL")
    for result in report.results:
        typer.echo(
            f"{result.key:<28} {result.status.value:<9} {result.requests:>3}  {result.error or '-'}"
        )
    typer.echo(f"overall: {report.status.value} ({report.requests} requests)")
    if report.status is SourceStatus.BROKEN:
        raise typer.Exit(code=1)


def _echo_dispatch(report: DispatchReport) -> None:
    typer.echo(
        f"dispatch: sent={report.sent} succeeded={report.succeeded} failed={report.failed} "
        f"skipped_unsafe={report.skipped_unsafe}"
    )


@webhooks_app.command("dispatch")
def webhooks_dispatch_command() -> None:
    """Make one delivery pass over every webhook event that is due."""
    _echo_dispatch(_run_dispatch(get_settings()))


def _refresh_outcome(settings: Settings) -> JobOutcome:
    summary = _run_refresh(settings)
    detail = {"run_id": summary.run_id, "run_status": summary.status, "requests": summary.requests}
    return summary.status != "failed", detail


def _dispatch_outcome(settings: Settings) -> JobOutcome:
    return True, asdict(_run_dispatch(settings))


def _emit_json(record: dict[str, object]) -> None:
    stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    typer.echo(json.dumps({"ts": stamp, **record}, sort_keys=True, default=str))


@app.command("worker")
def worker_command(
    refresh_every_minutes: Annotated[
        int, typer.Option(min=1, help="Minutes between refreshes.")
    ] = 360,
    dispatch_every_seconds: Annotated[
        int, typer.Option(min=1, help="Seconds between webhook dispatch passes.")
    ] = 30,
    once: Annotated[bool, typer.Option(help="One refresh and one dispatch, then exit.")] = False,
) -> None:
    """Refresh data and deliver webhooks on a schedule. Stops on SIGTERM or Ctrl-C.

    With --once, exits 1 if a job failed (handy for cron).
    """
    settings = get_settings()
    jobs = [
        ScheduledJob("refresh", refresh_every_minutes * 60, lambda: _refresh_outcome(settings)),
        ScheduledJob("dispatch", dispatch_every_seconds, lambda: _dispatch_outcome(settings)),
    ]
    stop = threading.Event()
    with _sigterm_stops(stop):
        ok = run_worker(jobs, emit=_emit_json, once=once, sleep=stop.wait, should_stop=stop.is_set)
    if not ok:
        raise typer.Exit(code=1)


@contextmanager
def _sigterm_stops(stop: threading.Event) -> Iterator[None]:
    """Make SIGTERM request a clean stop. Only the main thread may install handlers."""
    try:
        previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except ValueError:  # pragma: no cover - not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
