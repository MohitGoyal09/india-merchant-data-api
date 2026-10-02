"""A small Messages-API agent that answers merchant questions through MCP tools."""

from imda.agent.loop import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    AgentRun,
    AnthropicClient,
    RefusalInfo,
    TokenUsage,
    ToolCall,
    has_credentials,
    make_client,
    run_agent,
)
from imda.agent.mcp_backend import McpStdioBackend, ToolBackend, ToolOutcome, ToolSpec
from imda.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from imda.agent.transcript import format_tool_call, render_markdown

__all__ = [
    "DEFAULT_EFFORT",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MODEL",
    "SYSTEM_PROMPT",
    "AgentRun",
    "AnthropicClient",
    "McpStdioBackend",
    "RefusalInfo",
    "TokenUsage",
    "ToolBackend",
    "ToolCall",
    "ToolOutcome",
    "ToolSpec",
    "build_system_prompt",
    "format_tool_call",
    "has_credentials",
    "make_client",
    "render_markdown",
    "run_agent",
]
