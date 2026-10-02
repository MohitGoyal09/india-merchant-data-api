"""Helpers shared by the MCP contract evals."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient
from mcp_types import CallToolResult, TextContent

from imda.api.app import create_app
from imda.config import Settings
from mcp import Client
from tests.mcp.conftest import FRESH_NOW

BOILERPLATE = ("provenance", "warnings")
READ_ONLY_FLAGS = {
    "read_only_hint": True,
    "idempotent_hint": True,
    "destructive_hint": False,
    "open_world_hint": False,
}
TOOL_NAMES = {
    "fetch_all_offices",
    "fetch_holidays",
    "check_business_day",
    "fetch_next_business_days",
    "estimate_settlement_date",
    "quote_invoice",
    "fetch_fx_rate",
    "fetch_all_fx_rates",
    "convert_currency",
    "fetch_fx_stats",
    "compare_fx_sources",
    "fetch_mibor",
    "fetch_source_health",
}


async def call(client: Client, name: str, **arguments: Any) -> CallToolResult:
    return await client.call_tool(name, arguments)


async def ok(client: Client, name: str, **arguments: Any) -> dict[str, Any]:
    """The structuredContent of a call that must succeed."""
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return dict(result.structured_content)


def text_of(result: CallToolResult) -> str:
    first = result.content[0]
    assert isinstance(first, TextContent)
    return first.text


def error_of(result: CallToolResult) -> dict[str, Any]:
    """The JSON error body of a call that must have failed."""
    assert result.is_error, text_of(result)
    assert result.structured_content is None
    body = json.loads(text_of(result))
    assert set(body) == {"code", "message", "hint"}
    assert body["message"]
    assert body["hint"]
    return dict(body)


def data_of(structured: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in structured.items() if k not in BOILERPLATE}


def rest_client(settings: Settings, now: Any = lambda: FRESH_NOW) -> TestClient:
    return TestClient(create_app(settings, now=now))
