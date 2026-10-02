"""Ask the merchant-support agent one question. It calls our MCP server's tools to answer.

uv run python scripts/agent_demo.py "Is 2 October a bank holiday in Chennai?"
uv run python scripts/agent_demo.py --fixtures "Is 31 March 2026 a holiday in Mumbai?"
uv run python scripts/agent_demo.py --db data/imda.sqlite3 "..." --save docs/demo/chennai.md

Exit codes: 0 answered, 1 the run did not finish (refusal, error, turn limit), 2 no credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from imda.agent import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    AgentRun,
    AnthropicClient,
    McpStdioBackend,
    ToolBackend,
    ToolCall,
    build_system_prompt,
    format_tool_call,
    has_credentials,
    make_client,
    render_markdown,
    run_agent,
)
from seed_fixtures import seed

FIXTURE_NOW = "2026-09-30T15:00:00+05:30"
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2

BackendFactory = Callable[[Path | None, str | None], AbstractAsyncContextManager[ToolBackend]]


def default_backend(
    db_path: Path | None, fixed_now: str | None
) -> AbstractAsyncContextManager[ToolBackend]:
    return McpStdioBackend(db_path=db_path, fixed_now=fixed_now)


def _on_tool_call(call: ToolCall) -> None:
    print(f"  tool  {format_tool_call(call)}", flush=True)


def _print_run(run: AgentRun) -> None:
    print()
    if run.final_text:
        print(run.final_text)
    if run.error:
        print(f"\nstopped: {run.error}", file=sys.stderr)
    usage = run.usage
    served = run.model_served or "unknown model"
    print(
        f"\n[{served}; {run.turns} turn(s); {usage.input_tokens} input / "
        f"{usage.output_tokens} output tokens]",
        file=sys.stderr,
    )


async def _answer(
    args: argparse.Namespace,
    client: AnthropicClient,
    backend_factory: BackendFactory,
    db_path: Path | None,
    fixed_now: str | None,
) -> AgentRun:
    async with backend_factory(db_path, fixed_now) as backend:
        return await run_agent(
            args.question,
            backend,
            client,
            model=args.model,
            effort=args.effort,
            fallback=not args.no_fallback,
            max_turns=args.max_turns,
            system=build_system_prompt(fixed_now),
            on_tool_call=_on_tool_call,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ask the agent one merchant question.")
    parser.add_argument("question", help="the question to ask, in plain English")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--db", type=Path, help="SQLite DB for the MCP server")
    source.add_argument(
        "--fixtures", action="store_true", help="use a temp DB seeded from the recorded fixtures"
    )
    parser.add_argument("--fixed-now", metavar="ISO", help="fix the server clock (with offset)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT, choices=("low", "medium", "high"))
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--no-fallback", action="store_true", help="turn off refusal fallback")
    parser.add_argument("--save", type=Path, metavar="PATH", help="write a markdown transcript")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], AnthropicClient] = make_client,
    backend_factory: BackendFactory = default_backend,
) -> int:
    args = _parser().parse_args(argv)
    client = client_factory()
    if not has_credentials(client):
        print(
            "no Claude credentials found: set ANTHROPIC_API_KEY or run `ant auth login`",
            file=sys.stderr,
        )
        return EXIT_NO_CREDENTIALS

    fixed_now = args.fixed_now or (FIXTURE_NOW if args.fixtures else None)
    print(f"question: {args.question}", flush=True)
    if args.fixtures:
        with tempfile.TemporaryDirectory(prefix="imda-demo-") as tmp:
            db_path = Path(tmp) / "imda.sqlite3"
            seed(db_path)
            run = asyncio.run(_answer(args, client, backend_factory, db_path, fixed_now))
    else:
        run = asyncio.run(_answer(args, client, backend_factory, args.db, fixed_now))

    _print_run(run)
    if args.save is not None:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(render_markdown(run, model=args.model), encoding="utf-8")
        print(f"transcript saved to {args.save}", file=sys.stderr)
    return 0 if run.completed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
