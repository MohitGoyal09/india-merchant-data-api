"""Human-readable output for an agent run: live tool-call lines and a markdown transcript."""

from __future__ import annotations

import datetime as dt
import json

from imda.agent.loop import AgentRun, ToolCall

RESULT_PREVIEW_CHARS = 600
ARGS_PREVIEW_CHARS = 160


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def format_args(args: dict[str, object]) -> str:
    return _clip(json.dumps(args, ensure_ascii=False, sort_keys=True), ARGS_PREVIEW_CHARS)


def format_tool_call(call: ToolCall) -> str:
    """One line for the terminal: ``name(args)  ok|ERROR  123 ms``."""
    status = "ERROR" if call.is_error else "ok"
    return f"{call.name}({format_args(call.args)})  {status}  {call.duration_ms} ms"


def render_markdown(run: AgentRun, *, model: str, generated_at: dt.datetime | None = None) -> str:
    """A markdown transcript: question, tool calls with trimmed results, answer, tokens, model."""
    stamp = (generated_at or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Agent transcript",
        "",
        f"_Generated {stamp}_",
        "",
        "## Question",
        "",
        run.question,
        "",
        f"## Tool calls ({len(run.tool_calls)})",
        "",
    ]
    if not run.tool_calls:
        lines += ["No tools were called.", ""]
    for number, call in enumerate(run.tool_calls, start=1):
        status = "error" if call.is_error else "ok"
        lines += [
            f"### {number}. `{call.name}` ({status}, {call.duration_ms} ms)",
            "",
            "```json",
            json.dumps(call.args, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "Result (trimmed):",
            "",
            "```text",
            _clip(call.result, RESULT_PREVIEW_CHARS),
            "```",
            "",
        ]
    lines += ["## Answer", ""]
    lines += [run.final_text or "_No answer text._", ""]
    if run.error:
        lines += [f"> Run did not finish normally: {run.error}", ""]
    usage = run.usage
    lines += [
        "## Run details",
        "",
        f"- Model requested: `{model}`",
        f"- Model served: `{run.model_served or 'unknown'}`"
        + (" (refusal fallback ran)" if run.fallback_used else ""),
        f"- Stop reason: `{run.stop_reason}` after {run.turns} turn(s)",
        f"- Tokens: {usage.input_tokens} input, {usage.output_tokens} output, "
        f"{usage.cache_creation_input_tokens} cache write, "
        f"{usage.cache_read_input_tokens} cache read",
        "",
    ]
    return "\n".join(lines)
