"""Tool backends for the agent loop: a small Protocol plus an MCP-over-stdio implementation.

``McpStdioBackend`` spawns ``uv run imda mcp`` and talks to it with the MCP 2.x client SDK
(``mcp.Client`` over ``StdioServerParameters``). The loop only sees ``ToolBackend``, so tests and
other hosts can plug in their own.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

import mcp_types

from mcp import Client, StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COMMAND = "uv"
DEFAULT_ARGS: tuple[str, ...] = ("run", "--quiet", "imda", "mcp")
DEFAULT_CALL_TIMEOUT_SECONDS = 60.0

ENV_DB_PATH = "IMDA_DB_PATH"
ENV_FIXED_NOW = "IMDA_MCP_FIXED_NOW"


@dataclass(frozen=True)
class ToolSpec:
    """One tool as the backend lists it."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_anthropic(self) -> dict[str, Any]:
        """The Messages API tool definition. The schema is passed through unchanged."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool call returned: the text, the structured JSON (if any) and the error flag."""

    text: str
    structured: Any = None
    is_error: bool = False

    def for_model(self) -> str:
        """The text sent back to the model as the ``tool_result`` content.

        Structured output is sent as compact JSON. A text summary is kept when it adds something
        beyond a JSON copy of the same data.
        """
        if self.structured is None:
            return self.text
        compact = json.dumps(self.structured, ensure_ascii=False, separators=(",", ":"))
        if not self.text.strip() or _same_json(self.text, self.structured):
            return compact
        return f"{self.text}\n{compact}"


def _same_json(text: str, structured: Any) -> bool:
    try:
        return bool(json.loads(text) == structured)
    except ValueError:
        return False


class ToolBackend(Protocol):
    """Anything that can list tools and run them by name."""

    async def list_tools(self) -> list[ToolSpec]: ...

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> ToolOutcome: ...


def _without_json_copy(text: str, structured: Any) -> str:
    """Drop a JSON copy of ``structured`` from ``text``: the whole text, or a tail after a blank
    line (our server writes ``summary + blank line + JSON`` for clients that ignore
    ``structuredContent``)."""
    if structured is None:
        return text
    if _same_json(text, structured):
        return ""
    position = text.find("\n\n")
    while position != -1:
        if _same_json(text[position + 2 :], structured):
            return text[:position]
        position = text.find("\n\n", position + 2)
    return text


def _text_of(result: mcp_types.CallToolResult) -> str:
    structured = result.structured_content
    parts = [
        _without_json_copy(block.text, structured)
        for block in result.content
        if isinstance(block, mcp_types.TextContent)
    ]
    return "\n".join(part for part in parts if part.strip())


class McpStdioBackend:
    """Run an MCP server as a subprocess and call its tools. Use as ``async with``."""

    def __init__(
        self,
        *,
        command: str = DEFAULT_COMMAND,
        args: Sequence[str] = DEFAULT_ARGS,
        db_path: Path | str | None = None,
        fixed_now: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: Path | str | None = REPO_ROOT,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        env: dict[str, str] = dict(extra_env or {})
        if db_path is not None:
            env[ENV_DB_PATH] = str(db_path)
        if fixed_now:
            env[ENV_FIXED_NOW] = fixed_now
        self._params = StdioServerParameters(command=command, args=list(args), env=env, cwd=cwd)
        self._call_timeout = call_timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    @property
    def server_env(self) -> dict[str, str]:
        """The extra environment variables the server process gets (never the API key)."""
        return dict(self._params.env or {})

    async def __aenter__(self) -> Self:
        stack = AsyncExitStack()
        try:
            self._client = await stack.enter_async_context(Client(self._params))
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            await stack.__aexit__(exc_type, exc, tb)

    def _connected(self) -> Client:
        if self._client is None:
            raise RuntimeError("McpStdioBackend must be used inside `async with`")
        return self._client

    async def list_tools(self) -> list[ToolSpec]:
        client = self._connected()
        specs: list[ToolSpec] = []
        cursor: str | None = None
        while True:
            page = await client.list_tools(cursor=cursor)
            specs.extend(
                ToolSpec(tool.name, tool.description or "", dict(tool.input_schema))
                for tool in page.tools
            )
            cursor = page.next_cursor
            if not cursor:
                return specs

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> ToolOutcome:
        client = self._connected()
        result = await client.call_tool(name, dict(args), read_timeout_seconds=self._call_timeout)
        return ToolOutcome(
            text=_text_of(result),
            structured=result.structured_content,
            is_error=bool(result.is_error),
        )
