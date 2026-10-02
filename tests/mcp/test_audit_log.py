"""MCP audit log: one JSON line per tool call on `imda.mcp.audit`, names only, never values."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from imda.mcp import context
from imda.mcp.context import ToolEnv
from imda.mcp.server import build_http_app
from mcp import Client

pytestmark = pytest.mark.anyio

AUDIT = "imda.mcp.audit"
SENTINEL = "SENTINEL-merchant-9f3a7c"
REPO = Path(__file__).resolve().parents[2]
FIELDS = {
    "event",
    "ts",
    "tool",
    "toolset",
    "outcome",
    "duration_ms",
    "result_bytes",
    "truncated",
    "arg_keys",
    "request_id",
    "transport",
}


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def audit() -> Iterator[list[logging.LogRecord]]:
    capture = _Capture()
    logger = logging.getLogger(AUDIT)  # the audit logger does not propagate to root
    logger.addHandler(capture)
    try:
        yield capture.records
    finally:
        logger.removeHandler(capture)


def lines(records: list[logging.LogRecord]) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in records if r.name == AUDIT]


async def test_ok_call_writes_one_complete_line(
    client: Client, audit: list[logging.LogRecord]
) -> None:
    await client.call_tool("check_business_day", {"date": "2026-03-28", "office": "mumbai"})

    (line,) = lines(audit)
    assert set(line) == FIELDS
    assert line["event"] == "mcp_tool_call"
    assert line["tool"] == "check_business_day"
    assert line["toolset"] == "calendar"
    assert line["outcome"] == "ok"
    assert line["arg_keys"] == ["date", "office"]
    assert line["truncated"] is False
    assert line["result_bytes"] > 0
    assert line["duration_ms"] >= 0
    assert line["ts"].endswith("+00:00")
    assert line["transport"] == "in-memory"


async def test_one_line_per_call(client: Client, audit: list[logging.LogRecord]) -> None:
    await client.call_tool("fetch_all_offices", {})
    await client.call_tool("fetch_source_health", {})
    await client.call_tool("fetch_all_offices", {})

    assert [x["tool"] for x in lines(audit)] == [
        "fetch_all_offices",
        "fetch_source_health",
        "fetch_all_offices",
    ]
    assert [x["toolset"] for x in lines(audit)] == ["calendar", "health", "calendar"]


async def test_tool_error_logs_its_code_and_never_the_value(
    client: Client, audit: list[logging.LogRecord]
) -> None:
    result = await client.call_tool(
        "check_business_day", {"date": "2026-03-28", "office": SENTINEL}
    )

    assert result.is_error
    (line,) = lines(audit)
    assert line["outcome"] == "OFFICE_NOT_FOUND"
    assert line["arg_keys"] == ["date", "office"]
    assert SENTINEL not in audit[0].getMessage()


async def test_validation_error_is_audited_without_values(
    client: Client, audit: list[logging.LogRecord]
) -> None:
    result = await client.call_tool(
        "check_business_day", {"date": SENTINEL, "office": "mumbai", "extra": SENTINEL}
    )

    assert result.is_error
    (line,) = lines(audit)
    assert line["outcome"] == "INVALID_REQUEST"
    assert line["tool"] == "check_business_day"
    assert line["arg_keys"] == ["date", "extra", "office"]
    assert SENTINEL not in audit[0].getMessage()


async def test_unknown_tool_is_audited(client: Client, audit: list[logging.LogRecord]) -> None:
    await client.call_tool("no_such_tool", {})

    (line,) = lines(audit)
    assert line["tool"] == "no_such_tool"
    assert line["toolset"] is None
    assert line["outcome"] == "INVALID_REQUEST"


async def test_hostile_argument_names_are_not_echoed(
    client: Client, audit: list[logging.LogRecord]
) -> None:
    hostile = f"key with {SENTINEL} and spaces"
    await client.call_tool("fetch_all_offices", {hostile: 1})

    (line,) = lines(audit)
    assert SENTINEL not in audit[0].getMessage()
    assert line["arg_keys"] == ["<invalid>"]


async def test_internal_error_outcome(
    client: Client, audit: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: ToolEnv) -> None:
        raise RuntimeError(f"leak {SENTINEL}")

    monkeypatch.setattr(ToolEnv, "request", boom)

    result = await client.call_tool("fetch_all_offices", {})

    assert result.is_error
    (line,) = lines(audit)
    assert line["outcome"] == "internal_error"
    assert SENTINEL not in audit[0].getMessage()


async def test_truncated_result_is_flagged(
    client: Client, audit: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context, "MAX_STRUCTURED_BYTES", 2_500)

    await client.call_tool("fetch_all_offices", {})

    (line,) = lines(audit)
    assert line["outcome"] == "ok"
    assert line["truncated"] is True


async def test_http_transport_and_request_id_header(
    server: MCPServer, audit: list[logging.LogRecord]
) -> None:
    token = "t" * 40
    app = build_http_app(server, token)
    async with app.router.lifespan_context(app):
        http_client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:8100",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "trace-abc-1"},
        )
        async with http_client:
            transport = streamable_http_client("http://127.0.0.1:8100/mcp", http_client=http_client)
            async with Client(transport) as client:
                await client.call_tool("fetch_all_offices", {})

    (line,) = lines(audit)
    assert line["transport"] == "http"
    assert line["request_id"] == "trace-abc-1"
    assert token not in audit[0].getMessage()


def _drain(stream: IO[str] | None, sink: list[str]) -> None:
    assert stream is not None
    for line in stream:
        sink.append(line)


def _answered(stdout_lines: list[str], ids: set[int]) -> bool:
    seen: set[int] = set()
    for line in stdout_lines:
        try:
            seen.add(json.loads(line).get("id"))
        except (ValueError, AttributeError):
            continue
    return ids <= seen


@pytest.mark.skipif(sys.platform == "win32", reason="posix subprocess pipes")
def test_stdio_stdout_is_protocol_only_and_audit_goes_to_stderr(db_path: Path) -> None:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "audit-eval", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "check_business_day",
                "arguments": {"date": "2026-03-28", "office": SENTINEL},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "fetch_all_offices", "arguments": {}},
        },
    ]
    env = {
        **os.environ,
        "IMDA_DB_PATH": str(db_path),
        "IMDA_MCP_FIXED_NOW": "2026-09-25T10:00:00+05:30",
        "IMDA_ADMIN_TOKEN": "",
        "IMDA_MCP_TOKEN": "",
    }
    code = "from imda.cli import app; app()"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO,
    )
    assert proc.stdin is not None
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_lines), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_lines), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not _answered(stdout_lines, {1, 2, 3}):
            time.sleep(0.05)
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        for reader in readers:
            reader.join(timeout=5)
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)

    out_lines = [ln for ln in stdout.splitlines() if ln.strip()]
    parsed = [json.loads(ln) for ln in out_lines]  # every stdout line is JSON ...
    assert parsed
    assert all(p.get("jsonrpc") == "2.0" for p in parsed)  # ... and JSON-RPC
    assert {p["id"] for p in parsed if "id" in p} >= {1, 2, 3}
    assert "mcp_tool_call" not in stdout

    audit_lines = [
        json.loads(ln) for ln in stderr.splitlines() if ln.startswith("{") and "mcp_tool_call" in ln
    ]
    assert [a["tool"] for a in audit_lines] == ["check_business_day", "fetch_all_offices"]
    assert [a["outcome"] for a in audit_lines] == ["OFFICE_NOT_FOUND", "ok"]
    assert all(a["transport"] == "stdio" for a in audit_lines)
    assert all(set(a) == FIELDS for a in audit_lines)
    assert SENTINEL not in stderr
