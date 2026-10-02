"""Export the MCP tool specification from the live server to ``docs/mcp_tool_spec.json``.

Usage:
    uv run python scripts/export_tool_spec.py [--out docs/mcp_tool_spec.json] [--check]

The server is built in memory and asked for its tools, resources and prompts through the SDK
client, so the file always matches the code. Output is deterministic: sorted keys, indent 2.
``--check`` writes nothing and exits 1 when the file on disk differs (for CI).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio

from imda.config import Settings
from imda.mcp.server import build_server
from imda.mcp.tools import ALL_TOOLSETS
from mcp import Client

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "mcp_tool_spec.json"
UNUSED_DB = Path("data/imda.sqlite3")
"""Listing tools never opens the database, so this path does not need to exist."""
EXIT_OUT_OF_DATE = 1


def _settings() -> Settings:
    return Settings(db_path=UNUSED_DB, _env_file=None)


def _dump(model: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    return dumped


def _tool_entry(tool: Any, toolset: str) -> dict[str, Any]:
    dumped = _dump(tool)
    return {
        "name": dumped["name"],
        "title": dumped.get("title"),
        "description": dumped.get("description"),
        "toolset": toolset,
        "annotations": dumped.get("annotations"),
        "input_schema": dumped.get("inputSchema"),
        "output_schema": dumped.get("outputSchema"),
    }


async def _tool_names(toolsets: frozenset[str]) -> list[str]:
    async with Client(build_server(_settings(), toolsets=toolsets)) as client:
        return [tool.name for tool in (await client.list_tools()).tools]


async def _toolset_by_tool() -> dict[str, str]:
    """Map each tool to its toolset by building a server with one toolset at a time."""
    mapping: dict[str, str] = {}
    for toolset in sorted(ALL_TOOLSETS):
        for name in await _tool_names(frozenset({toolset})):
            mapping[name] = toolset
    return mapping


async def build_spec() -> dict[str, Any]:
    """The tool specification as plain JSON data."""
    toolset_of = await _toolset_by_tool()
    async with Client(build_server(_settings())) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        prompts = (await client.list_prompts()).prompts
        info = client.server_info
        instructions = client.instructions
    if info is None:
        raise RuntimeError("server sent no server info")
    sorted_tools = sorted(tools, key=lambda t: t.name)
    return {
        "server": {"name": info.name, "version": info.version, "instructions": instructions},
        "tools": [_tool_entry(tool, toolset_of[tool.name]) for tool in sorted_tools],
        "resources": [_dump(r) for r in sorted(resources, key=lambda r: str(r.uri))],
        "prompts": [_dump(p) for p in sorted(prompts, key=lambda p: p.name)],
    }


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="file to write or check")
    parser.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 if the file is out of date"
    )
    args = parser.parse_args(argv)

    text = render(anyio.run(build_spec))
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(
                f"{args.out} is out of date. Run: uv run python scripts/export_tool_spec.py",
                file=sys.stderr,
            )
            return EXIT_OUT_OF_DATE
        print(f"{args.out} is up to date.")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
