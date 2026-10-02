"""Layer-1 contract eval: `uv run imda mcp` over stdio, as Claude Code or Desktop launches it."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import anyio
import pytest
from mcp.client.stdio import StdioServerParameters

from mcp import Client
from tests.mcp.helpers import TOOL_NAMES

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[2]
BUDGET_SECONDS = 10.0


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
async def test_stdio_server_lists_tools_and_answers_a_call(db_path: Path) -> None:
    params = StdioServerParameters(
        command="uv",
        args=["run", "--quiet", "imda", "mcp"],
        cwd=str(REPO),
        env={
            **os.environ,
            "IMDA_DB_PATH": str(db_path),
            "IMDA_MCP_FIXED_NOW": "2026-09-25T10:00:00+05:30",
            "IMDA_ADMIN_TOKEN": "",
            "IMDA_MCP_TOKEN": "",
        },
    )
    started = time.monotonic()

    with anyio.fail_after(BUDGET_SECONDS):
        async with Client(params) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "check_business_day", {"date": "2026-03-28", "office": "mumbai"}
            )
            stale = await client.call_tool(
                "fetch_fx_rate", {"currency": "USD", "date": "2026-09-24"}
            )

    assert {t.name for t in tools.tools} == TOOL_NAMES
    assert result.structured_content is not None
    assert result.structured_content["reason"] == "4th Saturday"
    assert stale.structured_content is not None
    assert stale.structured_content["provenance"][0]["stale"] is False
    assert time.monotonic() - started < BUDGET_SECONDS
