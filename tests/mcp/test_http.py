"""Layer-1 contract evals: streamable HTTP at /mcp behind a bearer token."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

from imda.mcp.auth import BearerAuthMiddleware, token_matches
from imda.mcp.server import build_http_app
from mcp import Client
from tests.mcp.helpers import TOOL_NAMES

pytestmark = pytest.mark.anyio

TOKEN = "t" * 40
BASE = "http://127.0.0.1:8100"
URL = f"{BASE}/mcp"
INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "contract-eval", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
async def app(server: MCPServer) -> AsyncIterator[Starlette]:
    http_app = build_http_app(server, TOKEN)
    async with http_app.router.lifespan_context(http_app):
        yield http_app


def raw_client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"X-Api-Key": TOKEN},
    ],
)
async def test_missing_or_wrong_token_is_401(app: Starlette, headers: dict[str, str]) -> None:
    async with raw_client(app) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert TOKEN not in response.text


async def test_unauthorised_requests_never_reach_the_tools(app: Starlette) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "fetch_all_offices", "arguments": {}},
    }
    async with raw_client(app) as client:
        response = await client.post("/mcp", json=call, headers=MCP_HEADERS)

    assert response.status_code == 401
    assert "offices" not in response.text


async def test_initialize_with_the_token_is_200(app: Starlette) -> None:
    async with raw_client(app) as client:
        response = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    payload = next(
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )
    result = json.loads(payload)["result"]
    assert result["serverInfo"]["name"] == "india-merchant-data"
    assert "read-only" in result["instructions"]


async def test_scheme_is_case_insensitive(app: Starlette) -> None:
    async with raw_client(app) as client:
        response = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": f"bearer {TOKEN}"},
        )

    assert response.status_code == 200


async def test_full_client_session_over_http(app: Starlette) -> None:
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=BASE,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    async with http_client:
        transport = streamable_http_client(URL, http_client=http_client)
        async with Client(transport) as client:
            tools = await client.list_tools()
            offices = await client.call_tool("fetch_all_offices", {})
            bad = await client.call_tool("fetch_holidays", {"office": "nowhere", "year": 2026})

    assert {t.name for t in tools.tools} == TOOL_NAMES
    assert offices.structured_content is not None
    assert offices.structured_content["count"] == 34
    assert bad.is_error


async def test_the_http_client_without_a_token_cannot_connect(app: Starlette) -> None:
    http_client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)

    async def connect() -> None:
        async with http_client, Client(streamable_http_client(URL, http_client=http_client)) as c:
            await c.list_tools()

    with pytest.raises(BaseExceptionGroup) as raised:
        await connect()

    assert "MCPError" in repr(raised.value.exceptions)


def test_token_matching_rules() -> None:
    assert token_matches(f"Bearer {TOKEN}", TOKEN)
    assert token_matches(f"BEARER  {TOKEN}  ", TOKEN)
    assert not token_matches(None, TOKEN)
    assert not token_matches("Bearer ", TOKEN)
    assert not token_matches(f"Bearer {TOKEN}.", TOKEN)
    assert not token_matches(f"Token {TOKEN}", TOKEN)


def test_middleware_refuses_an_empty_token() -> None:
    async def inner(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        raise AssertionError

    with pytest.raises(ValueError, match="token"):
        BearerAuthMiddleware(inner, "")


async def test_non_http_scopes_pass_through_the_guard() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    guard = BearerAuthMiddleware(inner, TOKEN)
    await guard({"type": "lifespan"}, None, None)  # type: ignore[arg-type]

    assert seen == ["lifespan"]


def test_middleware_refuses_a_short_token() -> None:
    async def inner(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        raise AssertionError

    with pytest.raises(ValueError, match="32"):
        BearerAuthMiddleware(inner, "t" * 31)
    BearerAuthMiddleware(inner, "t" * 32)


async def test_each_401_is_logged_without_the_header(
    app: Starlette, caplog: pytest.LogCaptureFixture
) -> None:
    secret_guess = "guess-" + "g" * 40
    with caplog.at_level("WARNING", logger="imda.mcp"):
        async with raw_client(app) as client:
            await client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": f"Bearer {secret_guess}"},
            )
            await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    records = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "imda.mcp"]
    assert len(records) == 2
    for record in records:
        message = record.getMessage()
        assert "POST" in message
        assert "/mcp" in message
        assert "401" in message
    everything = caplog.text
    assert secret_guess not in everything
    assert TOKEN not in everything
    assert "Bearer" not in everything


async def test_a_good_token_is_not_logged_as_a_failure(
    app: Starlette, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="imda.mcp"):
        async with raw_client(app) as client:
            await client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"},
            )

    assert [r for r in caplog.records if r.name == "imda.mcp"] == []
