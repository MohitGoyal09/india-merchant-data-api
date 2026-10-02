"""Command-line entry point: ``imda <command>``."""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from typing import Annotated

import typer

from imda import __version__
from imda.config import Settings, get_settings
from imda.http.client import PoliteClient
from imda.ingest.backfill import backfill
from imda.ingest.common import ExchangeLog, RunSummary
from imda.ingest.refresh import refresh
from imda.models import IST, Dataset
from imda.sources.base import HttpClient
from imda.store.repo import Store

app = typer.Typer(no_args_is_help=True, add_completion=False)

DATASET_NAMES: dict[str, Dataset] = {
    "offices": Dataset.OFFICES,
    "holidays": Dataset.HOLIDAYS,
    "fx": Dataset.FX,
    "mibor": Dataset.MIBOR,
}
RECENT_RUNS = 5


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


@app.command("refresh")
def refresh_command() -> None:
    """Fetch what is new: offices, holidays, FX and MIBOR."""
    settings = get_settings()
    with Store.open(settings.db_path) as store:
        log = ExchangeLog(store)
        with _open_client(settings, log) as client:
            summary = refresh(store, client, today=_today(), exchange_log=log)
    _report(summary)


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
