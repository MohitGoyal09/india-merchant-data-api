"""Suite-wide test settings."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _plain_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer/Rich colour and wrap help text on CI (GITHUB_ACTIONS). Keep CLI output plain and wide
    so assertions on help and error text behave the same locally and in CI."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("_TYPER_FORCE_DISABLE_TERMINAL", "1")
