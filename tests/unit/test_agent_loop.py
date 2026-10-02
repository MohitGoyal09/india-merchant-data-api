"""The agent loop, tool backend and demo script, tested with a fake client (no network)."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace, TracebackType
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from imda.agent import (
    SYSTEM_PROMPT,
    AgentRun,
    McpStdioBackend,
    TokenUsage,
    ToolCall,
    ToolOutcome,
    ToolSpec,
    build_system_prompt,
    format_tool_call,
    has_credentials,
    make_client,
    render_markdown,
    run_agent,
)
from imda.agent.loop import FALLBACK_BETA, MAX_TOOL_RESULT_CHARS

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
SERVER_FILE = REPO / "src" / "imda" / "mcp" / "server.py"


# --------------------------------------------------------------------------- fakes
def message(
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    *,
    usage: Mapping[str, Any] | None = None,
    model: str = "claude-opus-5-5",
    stop_details: Mapping[str, Any] | None = None,
) -> BetaMessage:
    """A real ``BetaMessage`` (so the loop sees the SDK's own block types)."""
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            **(usage or {}),
        },
    }
    return BetaMessage.model_validate(body)


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(name: str, args: dict[str, Any], block_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": args}


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


class _Endpoint:
    def __init__(self, owner: ScriptedClient, route: str) -> None:
        self._owner = owner
        self._route = route

    def create(self, **kwargs: Any) -> Any:
        return self._owner.handle(self._route, kwargs)


class ScriptedClient:
    """Returns scripted responses in order; raises an item that is an exception."""

    api_key = "sk-ant-test-not-real"

    def __init__(self, *script: Any) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.messages = _Endpoint(self, "plain")
        self.beta = SimpleNamespace(messages=_Endpoint(self, "beta"))

    def handle(self, route: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append({"route": route, **kwargs, "messages": list(kwargs["messages"])})
        if not self._script:
            raise AssertionError("the loop called the API more often than scripted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeBackend:
    """In-memory tools. ``results`` maps a tool name to an outcome, an exception or a function."""

    def __init__(self, results: Mapping[str, Any] | None = None) -> None:
        self.results = dict(results or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeBackend:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def list_tools(self) -> list[ToolSpec]:
        schema = {"type": "object", "properties": {"office": {"type": "string"}}}
        return [
            ToolSpec("fetch_all_offices", "List offices.", schema),
            ToolSpec("check_business_day", "Is a date a working day?", schema),
        ]

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append((name, dict(args)))
        result = self.results.get(name, ToolOutcome("ok", {"name": name}))
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(args)  # type: ignore[no-any-return]
        return result  # type: ignore[no-any-return]


def run(
    client: ScriptedClient,
    backend: FakeBackend | None = None,
    question: str = "q?",
    **kwargs: Any,
) -> AgentRun:
    return asyncio.run(run_agent(question, backend or FakeBackend(), client, **kwargs))


# --------------------------------------------------------------------------- loop basics
def test_tool_use_then_end_turn_returns_answer_and_replays_blocks() -> None:
    first = message([THINKING, tool_use("check_business_day", {"office": "mumbai"})], "tool_use")
    client = ScriptedClient(first, message([text("It is a holiday.")]))
    backend = FakeBackend({"check_business_day": ToolOutcome("", {"is_business_day": False})})

    result = run(client, backend)

    assert result.final_text == "It is a holiday."
    assert result.stop_reason == "end_turn"
    assert result.completed
    assert result.turns == 2
    assert [c.name for c in result.tool_calls] == ["check_business_day"]
    assert result.tool_calls[0].args == {"office": "mumbai"}
    assert backend.calls == [("check_business_day", {"office": "mumbai"})]
    second_messages = client.calls[1]["messages"]
    assert [m["role"] for m in second_messages] == ["user", "assistant", "user"]
    assert second_messages[1]["content"] is first.content  # full content, thinking block included
    assert second_messages[1]["content"][0].type == "thinking"
    assert second_messages[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": '{"is_business_day":false}',
        }
    ]


def test_request_shape_follows_the_opus_5_5_rules() -> None:
    client = ScriptedClient(message([text("hi")]))

    run(client, model="claude-opus-5-5", effort="medium")

    call = client.calls[0]
    assert call["route"] == "beta"
    assert call["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["model"] == "claude-opus-5-5"
    assert call["max_tokens"] == 16000
    assert call["output_config"] == {"effort": "medium"}
    assert "thinking" not in call
    assert "tool_choice" not in call
    assert "stream" not in call
    assert call["messages"] == [{"role": "user", "content": "q?"}]
    assert [t["name"] for t in call["tools"]] == ["fetch_all_offices", "check_business_day"]
    assert set(call["tools"][0]) == {"name", "description", "input_schema"}
    assert call["system"][0]["text"] == SYSTEM_PROMPT


def test_no_fallback_uses_the_plain_messages_route() -> None:
    client = ScriptedClient(message([text("hi")]))

    run(client, fallback=False)

    call = client.calls[0]
    assert call["route"] == "plain"
    assert "betas" not in call
    assert "fallbacks" not in call


def test_parallel_tool_use_blocks_are_answered_in_one_user_message() -> None:
    blocks = [
        tool_use("fetch_all_offices", {}, "toolu_a"),
        tool_use("check_business_day", {"office": "mumbai"}, "toolu_b"),
    ]
    client = ScriptedClient(message(blocks, "tool_use"), message([text("done")]))

    result = run(client)

    user_turns = [m for m in client.calls[1]["messages"] if m["role"] == "user"]
    assert len(user_turns) == 2  # the question, then ONE message holding both results
    results = user_turns[1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b"]
    assert all(r["type"] == "tool_result" for r in results)
    assert len(result.tool_calls) == 2


def test_tool_error_sets_is_error_on_the_result_and_the_call() -> None:
    err = ToolOutcome('{"code":"CALENDAR_DATA_MISSING"}', None, True)
    client = ScriptedClient(
        message([tool_use("check_business_day", {"office": "x"})], "tool_use"),
        message([text("not available")]),
    )

    result = run(client, FakeBackend({"check_business_day": err}))

    sent = client.calls[1]["messages"][2]["content"][0]
    assert sent["is_error"] is True
    assert "CALENDAR_DATA_MISSING" in sent["content"]
    assert result.tool_calls[0].is_error is True
    assert result.final_text == "not available"


def test_successful_result_has_no_is_error_key() -> None:
    client = ScriptedClient(
        message([tool_use("fetch_all_offices", {})], "tool_use"), message([text("ok")])
    )

    run(client)

    assert "is_error" not in client.calls[1]["messages"][2]["content"][0]


def test_backend_exception_becomes_an_error_result_not_a_crash() -> None:
    client = ScriptedClient(
        message([tool_use("fetch_all_offices", {})], "tool_use"), message([text("sorry")])
    )

    result = run(client, FakeBackend({"fetch_all_offices": TimeoutError("server stalled")}))

    sent = client.calls[1]["messages"][2]["content"][0]
    assert sent["is_error"] is True
    assert "TimeoutError" in sent["content"]
    assert result.tool_calls[0].is_error


def test_non_dict_tool_input_is_reported_as_an_error_result() -> None:
    bad = SimpleNamespace(type="tool_use", id="toolu_x", name="fetch_all_offices", input='{"a": 1}')
    response = SimpleNamespace(
        content=[bad], stop_reason="tool_use", usage=None, model="m", stop_details=None
    )
    client = ScriptedClient(response, message([text("ok")]))
    backend = FakeBackend()

    result = run(client, backend)

    assert backend.calls == []  # never called with a string
    assert result.tool_calls[0].is_error
    assert client.calls[1]["messages"][2]["content"][0]["is_error"] is True


def test_tool_result_text_is_clipped() -> None:
    big = ToolOutcome("x" * (MAX_TOOL_RESULT_CHARS + 500))
    client = ScriptedClient(
        message([tool_use("fetch_all_offices", {})], "tool_use"), message([text("ok")])
    )

    run(client, FakeBackend({"fetch_all_offices": big}))

    sent = client.calls[1]["messages"][2]["content"][0]["content"]
    assert len(sent) < MAX_TOOL_RESULT_CHARS + 50
    assert sent.endswith("[output truncated]")


def test_on_tool_call_callback_sees_each_call() -> None:
    seen: list[ToolCall] = []
    client = ScriptedClient(
        message([tool_use("fetch_all_offices", {})], "tool_use"), message([text("ok")])
    )

    run(client, on_tool_call=seen.append)

    assert [c.name for c in seen] == ["fetch_all_offices"]


# --------------------------------------------------------------------------- stop reasons
def test_refusal_reports_the_category_and_returns_no_text() -> None:
    refusal = message(
        [],
        "refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
    )

    result = run(ScriptedClient(refusal))

    assert result.stop_reason == "refusal"
    assert not result.completed
    assert result.refusal is not None
    assert result.refusal.category == "cyber"
    assert result.error is not None
    assert "cyber" in result.error
    assert result.final_text == ""


def test_refusal_with_null_details_is_still_a_refusal() -> None:
    result = run(ScriptedClient(message([], "refusal")))

    assert result.stop_reason == "refusal"
    assert result.refusal is not None
    assert result.refusal.category is None
    assert result.error is not None
    assert "unknown" in result.error


def test_max_tokens_stops_with_a_clear_message_and_keeps_partial_text() -> None:
    result = run(ScriptedClient(message([text("partial ans")], "max_tokens")))

    assert result.stop_reason == "max_tokens"
    assert result.final_text == "partial ans"
    assert result.error is not None
    assert "max_tokens" in result.error
    assert not result.completed


def test_unexpected_stop_reason_is_reported() -> None:
    result = run(ScriptedClient(message([text("x")], "pause_turn")))

    assert result.stop_reason == "unexpected"
    assert result.error is not None
    assert "pause_turn" in result.error


def test_turn_cap_stops_a_loop_that_never_finishes() -> None:
    script = [
        message([tool_use("fetch_all_offices", {}, f"toolu_{n}")], "tool_use") for n in range(5)
    ]
    client = ScriptedClient(*script)
    backend = FakeBackend()

    result = run(client, backend, max_turns=3)

    assert result.stop_reason == "max_turns"
    assert result.turns == 3
    assert len(client.calls) == 3
    assert len(backend.calls) == 2  # the third request is not executed: there is no turn left
    assert result.error is not None
    assert "3 turns" in result.error


# --------------------------------------------------------------------------- usage + model
def test_usage_accumulates_across_turns() -> None:
    first = message(
        [tool_use("fetch_all_offices", {})],
        "tool_use",
        usage={"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 300},
    )
    second = message(
        [text("done")],
        usage={"input_tokens": 40, "output_tokens": 30, "cache_read_input_tokens": 300},
    )

    result = run(ScriptedClient(first, second))

    usage = result.usage
    assert (usage.input_tokens, usage.output_tokens) == (140, 50)
    assert (usage.cache_creation_input_tokens, usage.cache_read_input_tokens) == (300, 300)
    assert usage.total == 790


def test_fallback_model_and_flag_are_reported() -> None:
    served = message(
        [
            {
                "type": "fallback",
                "from": {"model": "claude-opus-5-5"},
                "to": {"model": "claude-opus-4-8"},
                "trigger": {"type": "refusal"},
            },
            text("answer"),
        ],
        model="claude-opus-4-8",
    )

    result = run(ScriptedClient(served))

    assert result.model_served == "claude-opus-4-8"
    assert result.fallback_used is True
    assert result.final_text == "answer"


def test_plain_run_reports_the_served_model_and_no_fallback() -> None:
    result = run(ScriptedClient(message([text("a")])))

    assert result.model_served == "claude-opus-5-5"
    assert result.fallback_used is False


# --------------------------------------------------------------------------- API errors
def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_rate_limit_error_is_reported_with_retry_after() -> None:
    response = httpx2.Response(429, request=_request(), headers={"retry-after": "7"})
    error = anthropic.RateLimitError("slow down", response=response, body=None)

    result = run(ScriptedClient(error))

    assert result.stop_reason == "rate_limited"
    assert result.error is not None
    assert "429" in result.error
    assert "7" in result.error


def test_status_error_is_reported_with_the_http_status() -> None:
    response = httpx2.Response(500, request=_request())
    error = anthropic.InternalServerError("upstream broke", response=response, body=None)

    result = run(ScriptedClient(error))

    assert result.stop_reason == "api_error"
    assert result.error is not None
    assert "500" in result.error
    assert "upstream broke" in result.error


def test_connection_error_is_reported() -> None:
    result = run(ScriptedClient(anthropic.APIConnectionError(request=_request())))

    assert result.stop_reason == "connection_error"
    assert result.error is not None
    assert "could not reach" in result.error


# --------------------------------------------------------------------------- prompt + helpers
def test_system_prompt_carries_the_agent_rules() -> None:
    lowered = SYSTEM_PROMPT.lower()
    for phrase in ("tool call", "estimate", "skipped day", "data", "gst", "guess"):
        assert phrase in lowered
    assert "instructions" in lowered  # tool output is data, never instructions
    assert "2026-09-30" in build_system_prompt("2026-09-30T15:00:00+05:30")
    assert build_system_prompt() == SYSTEM_PROMPT


def test_for_model_prefers_compact_structured_json() -> None:
    structured = {"a": 1, "b": [1, 2]}
    assert ToolOutcome("", structured).for_model() == '{"a":1,"b":[1,2]}'
    assert (
        ToolOutcome(json.dumps(structured, indent=2), structured).for_model() == '{"a":1,"b":[1,2]}'
    )
    assert (
        ToolOutcome("A summary line.", structured).for_model()
        == 'A summary line.\n{"a":1,"b":[1,2]}'
    )
    assert ToolOutcome("plain text").for_model() == "plain text"


def test_a_json_copy_of_the_structured_output_is_dropped_from_the_text() -> None:
    from imda.agent.mcp_backend import _without_json_copy

    structured = {"a": 1}
    summary = "Summary line."

    assert _without_json_copy('{"a": 1}', structured) == ""
    assert _without_json_copy(f'{summary}\n\n{{"a":1}}', structured) == summary
    assert _without_json_copy(f'{summary}\n\n{{"a":2}}', structured).endswith('{"a":2}')
    assert _without_json_copy(summary, None) == summary


def test_has_credentials_and_make_client(monkeypatch: pytest.MonkeyPatch) -> None:
    assert has_credentials(SimpleNamespace(api_key="k", auth_token=None, credentials=None))
    assert has_credentials(SimpleNamespace(api_key=None, auth_token="t", credentials=None))
    assert has_credentials(SimpleNamespace(api_key=None, auth_token=None, credentials=object()))
    assert not has_credentials(SimpleNamespace(api_key=None, auth_token=None, credentials=None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    assert has_credentials(make_client())


def test_transcript_markdown_has_all_sections() -> None:
    call = ToolCall(
        "check_business_day", {"office": "mumbai"}, False, 12, '{"x": ' + "y" * 900 + "}"
    )
    agent_run = AgentRun(
        question="Is it a holiday?",
        final_text="Yes, it is.",
        tool_calls=(call,),
        turns=2,
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=123, output_tokens=45),
        model_served="claude-opus-5-5",
    )

    markdown = render_markdown(agent_run, model="claude-opus-5-5")

    for needle in ("Is it a holiday?", "check_business_day", "Yes, it is.", "claude-opus-5-5"):
        assert needle in markdown
    assert "123 input" in markdown
    assert "..." in markdown  # long results are trimmed
    assert "y" * 900 not in markdown
    assert format_tool_call(call).startswith('check_business_day({"office": "mumbai"})  ok')


# --------------------------------------------------------------------------- stdio backend
TOY_SERVER = textwrap.dedent(
    """
    import os
    from mcp.server.mcpserver import MCPServer

    app = MCPServer("toy")

    @app.tool()
    def fetch_all_offices() -> dict[str, str]:
        '''List offices.'''
        env = os.environ
        return {"db": env.get("IMDA_DB_PATH", ""), "now": env.get("IMDA_MCP_FIXED_NOW", "")}

    @app.tool()
    def explode() -> str:
        '''Always fails.'''
        raise ValueError("kaboom")

    if __name__ == "__main__":
        app.run("stdio")
    """
)


def test_stdio_backend_lists_tools_and_calls_one(tmp_path: Path) -> None:
    script = tmp_path / "toy_server.py"
    script.write_text(TOY_SERVER, encoding="utf-8")
    backend = McpStdioBackend(
        command=sys.executable,
        args=[str(script)],
        db_path=tmp_path / "x.db",
        fixed_now="2026-09-30T15:00:00+05:30",
        cwd=tmp_path,
    )

    async def scenario() -> tuple[list[ToolSpec], ToolOutcome, ToolOutcome]:
        async with backend:
            return (
                await backend.list_tools(),
                await backend.call_tool("fetch_all_offices", {}),
                await backend.call_tool("explode", {}),
            )

    specs, ok, failed = asyncio.run(scenario())

    assert {s.name for s in specs} == {"fetch_all_offices", "explode"}
    assert specs[0].to_anthropic().keys() == {"name", "description", "input_schema"}
    assert ok.is_error is False
    assert ok.structured == {"db": str(tmp_path / "x.db"), "now": "2026-09-30T15:00:00+05:30"}
    assert failed.is_error is True


def test_stdio_backend_refuses_use_outside_async_with() -> None:
    backend = McpStdioBackend()

    with pytest.raises(RuntimeError, match="async with"):
        asyncio.run(backend.list_tools())


@pytest.mark.skipif(
    not SERVER_FILE.exists(), reason="the MCP server (src/imda/mcp/server.py) is not built yet"
)
def test_real_mcp_server_lists_tools_and_returns_offices(tmp_path: Path) -> None:
    seed_fixtures = _load_script("seed_fixtures")
    db_path = tmp_path / "imda.sqlite3"
    seed_fixtures.seed(db_path)
    backend = McpStdioBackend(db_path=db_path, fixed_now="2026-09-30T15:00:00+05:30")

    async def scenario() -> tuple[list[ToolSpec], ToolOutcome]:
        async with backend:
            return await backend.list_tools(), await backend.call_tool("fetch_all_offices", {})

    specs, offices = asyncio.run(scenario())

    names = {s.name for s in specs}
    assert len(specs) == 13
    assert {"fetch_all_offices", "quote_invoice", "fetch_mibor", "fetch_source_health"} <= names
    assert offices.is_error is False
    assert "mumbai" in offices.for_model()


# --------------------------------------------------------------------------- demo script
def _load_script(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture
def demo() -> ModuleType:
    return _load_script("agent_demo")


def test_demo_exits_2_without_credentials(
    demo: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    nobody = SimpleNamespace(api_key=None, auth_token=None, credentials=None)

    code = demo.main(["Is it a holiday?"], client_factory=lambda: nobody)

    assert code == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert "ant auth login" in err


def test_demo_prints_tool_calls_and_answer_and_saves_a_transcript(
    demo: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = ScriptedClient(
        message([tool_use("check_business_day", {"office": "mumbai"})], "tool_use"),
        message([text("31 March 2026 is a holiday (Mahavir Jayanti).")]),
    )
    backend_factory: Callable[[Any, Any], FakeBackend] = lambda db, now: FakeBackend()  # noqa: E731
    saved = tmp_path / "demo" / "mumbai.md"

    code = demo.main(
        ["Is 31 March a holiday?", "--fixtures", "--save", str(saved)],
        client_factory=lambda: client,
        backend_factory=backend_factory,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert 'check_business_day({"office": "mumbai"})' in captured.out
    assert "ok" in captured.out
    assert "Mahavir Jayanti" in captured.out
    markdown = saved.read_text(encoding="utf-8")
    assert "Is 31 March a holiday?" in markdown
    assert "check_business_day" in markdown
    assert "Mahavir Jayanti" in markdown
    assert "sk-ant" not in captured.out + captured.err + markdown  # a key is never printed
    assert "2026-09-30T15:00:00+05:30" in client.calls[0]["system"][0]["text"]  # --fixtures clock


def test_demo_exits_1_when_the_run_does_not_finish(demo: ModuleType) -> None:
    client = ScriptedClient(
        message([], "refusal", stop_details={"type": "refusal", "category": "bio"})
    )

    code = demo.main(
        ["q"], client_factory=lambda: client, backend_factory=lambda db, now: FakeBackend()
    )

    assert code == 1


# --------------------------------------------------------------------------- JSON-aware clipping
def _fx_page(target_bytes: int) -> dict[str, Any]:
    row = {
        "date": "2026-09-01",
        "currency": "USD",
        "rate": "88.1234",
        "unit": 1,
        "rate_per_unit": "88.1234",
        "source": "fbil",
    }
    one = len(json.dumps(row, separators=(",", ":"))) + 1
    rows = [
        {**row, "date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"}
        for i in range(target_bytes // one)
    ]
    return {
        "currency": "USD",
        "count": len(rows),
        "next_cursor": "2027-12-31.opaque",
        "rates": rows,
        "provenance": [{"source": "fbil", "dataset": "fx", "stale": False}],
        "warnings": ["page is full; continue with next_cursor"],
    }


def _sent_text(client: ScriptedClient) -> str:
    content = client.calls[1]["messages"][2]["content"][0]["content"]
    assert isinstance(content, str)
    return content


def _run_with(outcome: ToolOutcome) -> str:
    client = ScriptedClient(
        message([tool_use("fetch_all_offices", {})], "tool_use"), message([text("ok")])
    )
    run(client, FakeBackend({"fetch_all_offices": outcome}))
    return _sent_text(client)


def test_the_cap_sits_above_the_servers_text_cap() -> None:
    assert MAX_TOOL_RESULT_CHARS == 64_000


def test_a_36_kb_fx_page_reaches_the_model_whole() -> None:
    page = _fx_page(36_700)
    assert 36_000 < len(json.dumps(page, separators=(",", ":"))) < 40_000

    sent = _run_with(ToolOutcome("", page))

    assert json.loads(sent) == page
    assert json.loads(sent)["next_cursor"] == "2027-12-31.opaque"


def test_oversized_structured_result_drops_rows_and_keeps_every_other_key() -> None:
    page = _fx_page(150_000)

    sent = _run_with(ToolOutcome("", page))

    parsed = json.loads(sent)  # still valid JSON
    assert len(sent) <= MAX_TOOL_RESULT_CHARS
    assert parsed["next_cursor"] == "2027-12-31.opaque"
    assert parsed["provenance"] == page["provenance"]
    assert parsed["warnings"] == page["warnings"]
    assert parsed["count"] == page["count"]
    kept = len(parsed["rates"])
    assert 0 < kept < len(page["rates"])
    assert parsed["rates"] == page["rates"][:kept]  # trailing rows are the ones dropped
    assert parsed["truncated_rows"] == len(page["rates"]) - kept


def test_oversized_structured_result_keeps_the_summary_line() -> None:
    page = _fx_page(150_000)

    sent = _run_with(ToolOutcome("USD rates, 1 Jan to 31 Dec.", page))

    summary, _, body = sent.partition("\n")
    assert summary == "USD rates, 1 Jan to 31 Dec."
    assert json.loads(body)["next_cursor"] == "2027-12-31.opaque"
    assert len(sent) <= MAX_TOOL_RESULT_CHARS


def test_oversized_plain_text_is_cut_with_a_truncated_marker() -> None:
    sent = _run_with(ToolOutcome("y" * (MAX_TOOL_RESULT_CHARS * 2)))

    assert len(sent) <= MAX_TOOL_RESULT_CHARS + 50
    assert sent.endswith("[output truncated]")


def test_oversized_structured_result_without_a_list_falls_back_to_a_text_cut() -> None:
    sent = _run_with(ToolOutcome("", {"blob": "z" * (MAX_TOOL_RESULT_CHARS * 2)}))

    assert sent.endswith("[output truncated]")
    assert len(sent) <= MAX_TOOL_RESULT_CHARS + 50
