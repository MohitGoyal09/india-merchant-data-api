"""`imda mcp`: option handling, the HTTP token requirement, loopback default."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
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
            "--allowed-host",
            "mcp.example.com",
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


# ------------------------------------------------------------ non-loopback HTTP
INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "cli-test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "mcp.example.com"])
def test_non_loopback_host_without_allowed_host_exits_2(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, host: str
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings_with(db_path, TOKEN))
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("must not start"))

    result = runner.invoke(cli.app, ["mcp", "--transport", "http", "--host", host])

    assert result.exit_code == 2
    assert "--allowed-host" in result.output


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_hosts_need_no_allowed_host(served: dict[str, Any], host: str) -> None:
    result = runner.invoke(cli.app, ["mcp", "--transport", "http", "--host", host])

    assert result.exit_code == 0, result.output
    assert "TLS" not in result.output


@pytest.mark.parametrize("bad", ["", "https://mcp.example.com", "mcp.example.com/mcp", "a b"])
def test_malformed_allowed_host_is_a_usage_error(served: dict[str, Any], bad: str) -> None:
    result = runner.invoke(
        cli.app, ["mcp", "--transport", "http", "--host", "0.0.0.0", "--allowed-host", bad]
    )

    assert result.exit_code == 2
    assert "--allowed-host" in result.output


def test_non_loopback_with_allowed_host_starts_and_warns_about_tls(
    served: dict[str, Any],
) -> None:
    result = runner.invoke(
        cli.app,
        ["mcp", "--transport", "http", "--host", "0.0.0.0", "--allowed-host", "mcp.example.com"],
    )

    assert result.exit_code == 0, result.output
    assert served["kwargs"]["host"] == "0.0.0.0"
    warnings = [line for line in result.output.splitlines() if "TLS" in line]
    assert len(warnings) == 1
    assert "proxy" in warnings[0].lower()
    assert TOKEN not in result.output


@pytest.mark.anyio
async def test_started_app_rejects_a_wrong_host_and_accepts_the_right_one(
    served: dict[str, Any],
) -> None:
    result = runner.invoke(
        cli.app,
        [
            "mcp",
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--allowed-host",
            "mcp.example.com",
            "--allowed-host",
            "10.0.0.5:8100",
        ],
    )
    assert result.exit_code == 0, result.output
    app = served["app"]
    auth = {"Authorization": f"Bearer {TOKEN}"}

    async def post(host: str, headers: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://placeholder"
        ) as client:
            return await client.post(
                "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers, "Host": host}
            )

    async with app.router.lifespan_context(app):
        wrong = await post("evil.example.net", auth)
        wrong_port = await post("10.0.0.5:9999", auth)
        right = await post("mcp.example.com", auth)
        right_with_port = await post("10.0.0.5:8100", auth)
        no_token = await post("mcp.example.com", {})

    assert wrong.status_code in (403, 421)
    assert wrong_port.status_code in (403, 421)
    assert right.status_code == 200
    assert right_with_port.status_code == 200
    assert no_token.status_code == 401
