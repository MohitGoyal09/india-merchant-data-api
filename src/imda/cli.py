"""Command-line entry point: ``imda <command>``."""

from __future__ import annotations

import typer

from imda import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """India Merchant Data API: ingest, serve and check RBI + FBIL data."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
