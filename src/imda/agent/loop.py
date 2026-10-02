"""The tool-use loop: ask Claude, run the tools it calls, send the results back, repeat.

Written against the Messages API with Claude Opus 5.5 rules (see docs/PLAN_MCP.md section 4):

- ``thinking`` is not sent (adaptive thinking is always on); ``output_config.effort`` is explicit.
- ``tool_choice`` is left at ``auto``. The system prompt steers tool use.
- The full ``response.content`` (thinking blocks included) goes back unchanged each turn.
- All ``tool_result`` blocks for one assistant turn go in one user message; tool errors set
  ``is_error``.
- ``stop_reason`` drives the loop: ``tool_use`` runs tools, ``end_turn`` finishes, ``refusal`` and
  ``max_tokens`` stop with a clear result.
- Server-side refusal fallback (``fallbacks="default"``) is on unless the caller turns it off.

The client is injected, so tests pass a fake with scripted responses.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import anthropic

from imda.agent.mcp_backend import ToolBackend, ToolOutcome, same_json
from imda.agent.prompts import build_system_prompt

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TURNS = 10
MAX_TOKENS = 16_000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_RESULT_CHARS = 64_000
"""Above the MCP server's own caps (about 48 KB of text, 48 KB of structured JSON)."""
TRUNCATED_MARKER = "\n[output truncated]"
EMPTY_RESULT_TEXT = "(the tool returned no content)"


# --------------------------------------------------------------------------- client
class MessagesApi(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class BetaApi(Protocol):
    @property
    def messages(self) -> MessagesApi: ...


class AnthropicClient(Protocol):
    """The slice of ``anthropic.Anthropic`` the loop uses. Tests pass a fake."""

    @property
    def messages(self) -> MessagesApi: ...

    @property
    def beta(self) -> BetaApi: ...


def make_client() -> AnthropicClient:
    """A zero-argument client: it resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ``ant``
    auth profile. Construction never fails for missing credentials; check ``has_credentials``."""
    return cast("AnthropicClient", anthropic.Anthropic())


def has_credentials(client: object) -> bool:
    """True when the client has an API key, an auth token or a credentials provider."""
    return any(getattr(client, name, None) for name in ("api_key", "auth_token", "credentials"))


# --------------------------------------------------------------------------- results
@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    is_error: bool
    duration_ms: int
    result: str = ""


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True)
class RefusalInfo:
    category: str | None
    explanation: str | None = None
    recommended_model: str | None = None


@dataclass(frozen=True)
class AgentRun:
    question: str
    final_text: str
    tool_calls: tuple[ToolCall, ...]
    turns: int
    stop_reason: str
    usage: TokenUsage
    model_served: str | None = None
    fallback_used: bool = False
    refusal: RefusalInfo | None = None
    error: str | None = None

    @property
    def completed(self) -> bool:
        """True when the model finished normally (``end_turn``)."""
        return self.stop_reason == "end_turn"


@dataclass
class _Tally:
    """Mutable bookkeeping for one run. Turned into a frozen ``AgentRun`` at the end."""

    question: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    calls: list[ToolCall] = field(default_factory=list)
    model_served: str | None = None
    fallback_used: bool = False

    def record_response(self, response: Any) -> None:
        self.usage = self.usage + _usage_of(response)
        self.model_served = getattr(response, "model", None) or self.model_served
        self.fallback_used = self.fallback_used or _fallback_ran(response)

    def result(
        self,
        stop_reason: str,
        turns: int,
        *,
        final_text: str = "",
        refusal: RefusalInfo | None = None,
        error: str | None = None,
    ) -> AgentRun:
        return AgentRun(
            question=self.question,
            final_text=final_text,
            tool_calls=tuple(self.calls),
            turns=turns,
            stop_reason=stop_reason,
            usage=self.usage,
            model_served=self.model_served,
            fallback_used=self.fallback_used,
            refusal=refusal,
            error=error,
        )


def _usage_of(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)

    def count(name: str) -> int:
        value = getattr(usage, name, None)
        return value if isinstance(value, int) else 0

    return TokenUsage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_creation_input_tokens=count("cache_creation_input_tokens"),
        cache_read_input_tokens=count("cache_read_input_tokens"),
    )


def _fallback_ran(response: Any) -> bool:
    """A ``fallback`` block or a ``fallback_message`` iteration means another model ran."""
    iterations = getattr(getattr(response, "usage", None), "iterations", None) or []
    if any(getattr(item, "type", None) == "fallback_message" for item in iterations):
        return True
    return any(getattr(block, "type", None) == "fallback" for block in response.content)


def _text_of(response: Any) -> str:
    texts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "\n".join(texts).strip()


def _refusal_of(response: Any) -> RefusalInfo:
    details = getattr(response, "stop_details", None)
    return RefusalInfo(
        category=getattr(details, "category", None),
        explanation=getattr(details, "explanation", None),
        recommended_model=getattr(details, "recommended_model", None),
    )


# --------------------------------------------------------------------------- API call
def build_request(
    *,
    model: str,
    effort: str,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """The request body. No ``thinking`` (adaptive is the default) and no ``tool_choice``."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "tools": tools,
        "messages": list(messages),
        "output_config": {"effort": effort},
    }


async def _call_model(client: AnthropicClient, request: dict[str, Any], *, fallback: bool) -> Any:
    def send() -> Any:
        if fallback:
            return client.beta.messages.create(
                **request, betas=[FALLBACK_BETA], fallbacks="default"
            )
        return client.messages.create(**request)

    return await asyncio.to_thread(send)


def _api_failure(exc: anthropic.APIError) -> tuple[str, str]:
    """Map an API exception to ``(stop_reason, message)``. Most specific type first."""
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = exc.response.headers.get("retry-after")
        wait = f"retry after {retry_after}s" if retry_after else "retry later"
        return "rate_limited", f"rate limited by the API (HTTP 429); {wait}"
    if isinstance(exc, anthropic.APIStatusError):
        return "api_error", f"API error (HTTP {exc.status_code}): {exc.message}"
    return "connection_error", f"could not reach the API: {exc.message}"


# --------------------------------------------------------------------------- tools
def _text_cut(text: str) -> str:
    return text[:MAX_TOOL_RESULT_CHARS] + TRUNCATED_MARKER


def _shrunk_json(outcome: ToolOutcome) -> str | None:
    """The result with trailing rows dropped so it fits, or None when that cannot work.

    Only the largest list field loses rows; every other key (``next_cursor`` included) is kept,
    and ``truncated_rows`` says how many rows went. The output stays parseable JSON.
    """
    data = outcome.structured
    if not isinstance(data, dict):
        return None
    lists = {k: v for k, v in data.items() if isinstance(v, list) and v}
    if not lists:
        return None
    key = max(lists, key=lambda k: len(json.dumps(lists[k], ensure_ascii=False)))
    rows = lists[key]
    summary = outcome.text.strip()
    head = "" if not summary or same_json(summary, data) else f"{summary}\n"

    def render(kept: int) -> str:
        body = {**data, key: rows[:kept], "truncated_rows": len(rows) - kept}
        return head + json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    if len(render(0)) > MAX_TOOL_RESULT_CHARS:
        return None
    low, high = 0, len(rows)  # the most rows that still fit: render(low) always fits
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(middle)) <= MAX_TOOL_RESULT_CHARS:
            low = middle
        else:
            high = middle - 1
    return render(low)


def _clip(outcome: ToolOutcome) -> str:
    text = outcome.for_model()
    if not text:
        return EMPTY_RESULT_TEXT
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return _shrunk_json(outcome) or _text_cut(text)


async def _execute_tool(backend: ToolBackend, block: Any) -> tuple[ToolCall, dict[str, Any]]:
    """Run one ``tool_use`` block. Never raises: a failure becomes an ``is_error`` result."""
    raw_args = block.input
    started = time.perf_counter()
    if isinstance(raw_args, dict):
        args: dict[str, Any] = raw_args
        try:
            outcome = await backend.call_tool(block.name, args)
        except Exception as exc:
            outcome = ToolOutcome(f"Tool call failed: {type(exc).__name__}: {exc}", None, True)
    else:
        args = {}
        outcome = ToolOutcome("Tool input must be a JSON object.", None, True)
    duration_ms = round((time.perf_counter() - started) * 1000)
    text = _clip(outcome)
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": text}
    if outcome.is_error:
        result["is_error"] = True
    return ToolCall(block.name, dict(args), outcome.is_error, duration_ms, text), result


async def _run_tools(
    backend: ToolBackend,
    response: Any,
    tally: _Tally,
    on_tool_call: Callable[[ToolCall], None] | None,
) -> list[dict[str, Any]]:
    blocks = [block for block in response.content if getattr(block, "type", None) == "tool_use"]
    executed = await asyncio.gather(*(_execute_tool(backend, block) for block in blocks))
    results: list[dict[str, Any]] = []
    for call, result in executed:
        tally.calls.append(call)
        results.append(result)
        if on_tool_call is not None:
            on_tool_call(call)
    return results


# --------------------------------------------------------------------------- the loop
async def run_agent(
    question: str,
    backend: ToolBackend,
    client: AnthropicClient,
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    fallback: bool = True,
    max_turns: int = DEFAULT_MAX_TURNS,
    system: str | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
) -> AgentRun:
    """Answer ``question`` with the backend's tools. Returns a result for every outcome; only
    programming errors raise."""
    tally = _Tally(question)
    tools = [spec.to_anthropic() for spec in await backend.list_tools()]
    system_prompt = system if system is not None else build_system_prompt()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    for turn in range(1, max_turns + 1):
        request = build_request(
            model=model, effort=effort, system=system_prompt, tools=tools, messages=messages
        )
        try:
            response = await _call_model(client, request, fallback=fallback)
        except anthropic.APIError as exc:
            reason, message = _api_failure(exc)
            return tally.result(reason, turn, error=message)
        tally.record_response(response)

        stop = response.stop_reason
        if stop == "refusal":
            refusal = _refusal_of(response)
            message = f"the model declined this request (category: {refusal.category or 'unknown'})"
            return tally.result("refusal", turn, refusal=refusal, error=message)
        if stop == "max_tokens":
            message = f"the answer was cut off at max_tokens ({MAX_TOKENS})"
            return tally.result("max_tokens", turn, final_text=_text_of(response), error=message)
        if stop == "end_turn":
            return tally.result("end_turn", turn, final_text=_text_of(response))
        if stop != "tool_use" or not any(b.type == "tool_use" for b in response.content):
            return tally.result(
                "unexpected",
                turn,
                final_text=_text_of(response),
                error=f"unexpected stop_reason: {stop!r}",
            )
        if turn == max_turns:
            return tally.result(
                "max_turns", turn, error=f"stopped after {max_turns} turns without a final answer"
            )

        results = await _run_tools(backend, response, tally, on_tool_call)
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": results})

    return tally.result("max_turns", max_turns, error=f"stopped after {max_turns} turns")
