"""Tool registration, one module per toolset."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import MCPServer

from imda.mcp.context import ToolEnv
from imda.mcp.tools import calendar, fx, health, rates, settlement

Register = Callable[[MCPServer, ToolEnv], None]
REGISTRARS: dict[str, Register] = {
    "calendar": calendar.register,
    "settlement": settlement.register,
    "fx": fx.register,
    "rates": rates.register,
    "health": health.register,
}
ALL_TOOLSETS = frozenset(REGISTRARS)
