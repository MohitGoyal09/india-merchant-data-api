"""`imda mcp`: option handling, the HTTP token requirement, loopback default."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import imda.cli as cli
from imda.config import Settings
from imda.mcp.auth import BearerAuthMiddleware

runner = CliRunner()
TOKEN = "k" * 36


def settings_with(db: Path, token: str | None) -> Settings:
    return Settings(db_path=db, mcp_token=token, _env_file=None)  # type: ignore[arg-type,call-arg]


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "get_settings", lambda: settings_with(db_path, TOKEN))
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda app, **kwargs: seen.update(app=app, kwargs=kwargs)
    )
    return seen


def test_http_defaults_to_loopback_port_8100_with_bearer_guard(served: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["mcp", "--transport", "http"])

    assert result.exit_code == 0, result.output
    assert served["kwargs"] == {"host": "127.0.0.1", "port": 8100}
    middleware = {m.cls for m in served["app"].user_middleware}
    assert BearerAuthMiddleware in middleware
    assert {getattr(r, "path", None) for r in served["app"].routes} >= {"/mcp"}


def test_http_host_port_and_toolsets_are_passed_through(served: dict[str, Any]) -> None:
    result = runner.invoke(
        cli.app,
        [
            "mcp",
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--toolsets",
            "fx, rates",
        ],
    )

    assert result.exit_code == 0, result.output
    assert served["kwargs"] == {"host": "0.0.0.0", "port": 9000}


def test_http_refuses_to_start_without_a_token(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings_with(db_path, None))
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda *a, **k: pytest.fail("must not start without a token")
    )

    result = runner.invoke(cli.app, ["mcp", "--transport", "http"])

    assert result.exit_code == 2
    assert "IMDA_MCP_TOKEN" in result.output


def test_a_short_token_is_rejected_by_settings(db_path: Path) -> None:
    with pytest.raises(ValueError, match="32"):
        settings_with(db_path, "short")


def test_an_empty_token_means_unset(db_path: Path) -> None:
    assert settings_with(db_path, "  ").mcp_token is None


def test_token_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("IMDA_MCP_TOKEN", TOKEN)

    configured = Settings(db_path=db_path, _env_file=None)  # type: ignore[call-arg]

    assert configured.mcp_token is not None
    assert configured.mcp_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(configured)


def test_unknown_toolset_is_a_usage_error(served: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["mcp", "--toolsets", "fx,bogus"])

    assert result.exit_code == 2
    assert "bogus" in result.output


def test_stdio_is_the_default_and_needs_no_token(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(cli, "get_settings", lambda: settings_with(db_path, None))
    monkeypatch.setattr(
        "imda.mcp.server.ImdaServer.run", lambda self, transport="stdio", **_: ran.append(transport)
    )

    result = runner.invoke(cli.app, ["mcp", "--toolsets", "calendar"])

    assert result.exit_code == 0, result.output
    assert ran == ["stdio"]
