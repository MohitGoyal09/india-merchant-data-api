"""Keyless MCP demo: start `imda mcp` over stdio and run a merchant conversation against it.

No API key and no network. The server reads a SQLite DB seeded from the recorded fixtures and
runs on a fixed clock. Every expected value comes from the REST layer (FastAPI TestClient) on
the same DB and clock, so the demo proves the MCP tools and the REST API agree.

uv run python scripts/demo_mcp.py                       # seed a temp DB, run all checks
uv run python scripts/demo_mcp.py --db data/imda.sqlite3   # use an existing DB as it is
uv run python scripts/demo_mcp.py --json report.json

Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from imda.api.app import create_app
from imda.config import Settings
from imda.models import IST
from mcp import Client, StdioServerParameters
from seed_fixtures import seed

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW_TEXT = "2026-09-30T15:00:00+05:30"
FIXED_NOW = dt.datetime.fromisoformat(FIXED_NOW_TEXT).astimezone(IST)
SERVER_COMMAND = "uv"
SERVER_ARGS = ("run", "--quiet", "imda", "mcp")
CALL_TIMEOUT_SECONDS = 60.0
PAGE_LIMIT = 5
MAX_PAGES = 50

TOOL_NAMES = frozenset(
    {
        "fetch_all_offices",
        "fetch_holidays",
        "check_business_day",
        "fetch_next_business_days",
        "estimate_settlement_date",
        "quote_invoice",
        "fetch_fx_rate",
        "fetch_all_fx_rates",
        "convert_currency",
        "fetch_fx_stats",
        "compare_fx_sources",
        "fetch_mibor",
        "fetch_source_health",
    }
)
CALENDAR_TOOLS = frozenset(
    {"fetch_all_offices", "fetch_holidays", "check_business_day", "fetch_next_business_days"}
)
FX_TOOLS = frozenset({"fetch_fx_rate", "fetch_all_fx_rates", "convert_currency", "fetch_fx_stats"})

Outcome = Literal["PASS", "FAIL"]


class CheckFailed(Exception):
    """One check did not hold. The message says what differed."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def same(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise CheckFailed(f"{label}: MCP gave {actual!r}, REST gave {expected!r}")


@dataclass(frozen=True)
class Result:
    n: int
    name: str
    tool: str
    result: Outcome
    ms: int
    failures: list[str] = field(default_factory=list)


class Oracle:
    """The REST layer on the same DB and clock: the source of every expected value."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def get(self, path: str, **params: str) -> Any:
        response = self._client.get(path, params=params)
        require(response.status_code == 200, f"REST {path} returned {response.status_code}")
        return response.json()

    def data(self, path: str, **params: str) -> Any:
        return self.get(path, **params)["data"]

    def post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._client.post(path, json=body)
        require(response.status_code == 200, f"REST {path} returned {response.status_code}")
        return response.json()["data"]

    def error_code(self, path: str, **params: str) -> str:
        response = self._client.get(path, params=params)
        require(response.status_code >= 400, f"REST {path} did not fail")
        return str(response.json()["error"]["code"])


@dataclass(frozen=True)
class Session:
    """What a check gets: a connected MCP client, the REST oracle and the DB path."""

    client: Client
    oracle: Oracle
    db_path: Path


async def ok(session: Session, tool: str, **arguments: Any) -> dict[str, Any]:
    """The structured content of a tool call that must succeed."""
    result = await session.client.call_tool(
        tool, arguments, read_timeout_seconds=CALL_TIMEOUT_SECONDS
    )
    first = result.content[0].text if result.content and hasattr(result.content[0], "text") else ""
    require(not result.is_error, f"{tool} returned an error: {first[:200]}")
    require(result.structured_content is not None, f"{tool} returned no structured content")
    return dict(result.structured_content or {})


async def fails(session: Session, tool: str, **arguments: Any) -> dict[str, Any]:
    """The JSON error body of a tool call that must fail: code, message and hint."""
    result = await session.client.call_tool(
        tool, arguments, read_timeout_seconds=CALL_TIMEOUT_SECONDS
    )
    require(result.is_error, f"{tool} should have failed")
    require(result.content, f"{tool} error has no text")
    body = json.loads(getattr(result.content[0], "text", ""))
    require(set(body) == {"code", "message", "hint"}, f"error body keys are {sorted(body)}")
    require(body["message"] and body["hint"], "error must carry a message and a hint")
    return dict(body)


# --------------------------------------------------------------------------- checks
async def check_tools_listed(session: Session) -> None:
    listed = await session.client.list_tools()
    names = {tool.name for tool in listed.tools}
    require(len(listed.tools) == 13, f"expected 13 tools, got {len(listed.tools)}")
    require(names == TOOL_NAMES, f"tool names differ: {sorted(names ^ TOOL_NAMES)}")


async def check_read_only(session: Session) -> None:
    listed = await session.client.list_tools()
    for tool in listed.tools:
        a = tool.annotations
        if a is None:
            raise CheckFailed(f"{tool.name} has no annotations")
        flags = (a.read_only_hint, a.idempotent_hint, a.destructive_hint, a.open_world_hint)
        require(flags == (True, True, False, False), f"{tool.name} annotations are {flags}")


async def check_resources(session: Session) -> None:
    listed = await session.client.list_resources()
    uris = {str(r.uri) for r in listed.resources}
    require({"imda://offices", "imda://sources/health"} <= uris, f"resources are {sorted(uris)}")
    contents = await session.client.read_resource("imda://offices")
    body = json.loads(getattr(contents.contents[0], "text", "{}"))
    same(body["count"], len(session.oracle.data("/v1/offices")), "offices resource count")


async def check_prompt(session: Session) -> None:
    listed = await session.client.list_prompts()
    require("settlement_answer" in {p.name for p in listed.prompts}, "settlement_answer missing")
    question = "When will my USD payment settle in Mumbai?"
    prompt = await session.client.get_prompt("settlement_answer", {"question": question})
    text = getattr(prompt.messages[0].content, "text", "")
    require(question in text, "prompt text does not contain the merchant question")
    require("quote_invoice" in text, "prompt does not point at quote_invoice")


async def check_offices(session: Session) -> None:
    got = await ok(session, "fetch_all_offices")
    expected = session.oracle.data("/v1/offices")
    same(got["count"], len(expected), "office count")
    same(sorted(o["slug"] for o in got["offices"]), sorted(o["slug"] for o in expected), "slugs")
    require("mumbai" in {o["slug"] for o in got["offices"]}, "mumbai is not listed")


async def check_holidays(session: Session) -> None:
    got = await ok(session, "fetch_holidays", office="mumbai", year=2026, month=3)
    expected = session.oracle.data("/v1/holidays", office="mumbai", year="2026", month="3")
    require(expected, "REST holiday list is empty")
    same([h["date"] for h in got["holidays"]], [h["date"] for h in expected], "holiday dates")
    same(got["count"], len(expected), "holiday count")


async def check_business_day(session: Session) -> None:
    got = await ok(session, "check_business_day", date="2026-03-31", office="mumbai")
    expected = session.oracle.data("/v1/calendar/business-day", date="2026-03-31", office="mumbai")
    require(got["is_business_day"] is False, "31 Mar 2026 should be a bank holiday in Mumbai")
    same(got["is_business_day"], expected["is_business_day"], "is_business_day")
    same(got["reason"], expected["reason"], "reason")
    require("Mahavir" in got["reason"], f"reason is {got['reason']!r}")


async def check_next_days(session: Session) -> None:
    got = await ok(session, "fetch_next_business_days", date="2026-03-27", office="mumbai", count=3)
    expected = session.oracle.data(
        "/v1/calendar/next-business-days", date="2026-03-27", n="3", office="mumbai"
    )
    same(got["dates"], expected["dates"], "next business days")


async def check_settlement(session: Session) -> None:
    got = await ok(
        session,
        "estimate_settlement_date",
        captured_at="2026-03-27T11:00:00+05:30",
        office="mumbai",
    )
    expected = session.oracle.data(
        "/v1/settlement/eta", captured_at="2026-03-27T11:00:00+05:30", office="mumbai"
    )
    same(got["eta_date"], expected["eta_date"], "eta_date")
    same(got["skipped"], expected["skipped"], "skipped days")
    require(got["eta_date"] == "2026-04-02", f"ETA is {got['eta_date']}")
    require(len(got["skipped"]) == 4, f"{len(got['skipped'])} days skipped, expected 4")


async def check_fx_fallback(session: Session) -> None:
    got = await ok(session, "fetch_fx_rate", currency="USD", date="2026-09-14")
    expected = session.oracle.data("/v1/fx/rates/as-of", currency="USD", date="2026-09-14")
    same(got["effective_date"], expected["effective_date"], "effective_date")
    same(got["rate"], expected["rate"], "rate row")
    same(got["reason"], expected["reason"], "reason")
    require(got["effective_date"] == "2026-09-11", f"effective date is {got['effective_date']}")
    require(any("stale" in w for w in got["warnings"]), "no stale warning on the old data")


async def check_convert_jpy(session: Session) -> None:
    got = await ok(
        session,
        "convert_currency",
        amount="1000",
        from_currency="JPY",
        to_currency="INR",
        date="2026-09-24",
    )
    expected = session.oracle.data(
        "/v1/fx/convert", amount="1000", **{"from": "JPY"}, to="INR", date="2026-09-24"
    )
    same(got["result"], expected["result"], "converted amount")
    unit = got["rates_used"][0]["rate"]["unit"]
    require(unit == 100, f"JPY rate unit is {unit}, expected 100")


async def check_convert_number(session: Session) -> None:
    as_text = await ok(
        session,
        "convert_currency",
        amount="1000",
        from_currency="JPY",
        to_currency="INR",
        date="2026-09-24",
    )
    as_number = await ok(
        session,
        "convert_currency",
        amount=1000,
        from_currency="JPY",
        to_currency="INR",
        date="2026-09-24",
    )
    same(as_number["result"], as_text["result"], "JSON-number amount vs string amount")
    same(as_number["amount"], "1000", "amount echoed back")


async def check_quote(session: Session) -> None:
    arguments = {
        "amount": "1200",
        "currency": "USD",
        "invoice_date": "2026-09-24",
        "captured_at": "2026-09-24T11:00:00+05:30",
        "office": "mumbai",
    }
    got = await ok(session, "quote_invoice", **arguments)
    expected = session.oracle.post("/v1/invoice/quote", arguments)
    same(got["conversion"]["result"], expected["conversion"]["result"], "INR amount")
    same(got["settlement"]["eta_date"], expected["settlement"]["eta_date"], "settlement ETA")
    require(got["notes"], "quote has no notes")


async def check_paging(session: Session) -> None:
    arguments = {"currency": "USD", "from_date": "2026-09-01", "to_date": "2026-09-24"}
    dates: list[str] = []
    pages = 0
    cursor: str | None = None
    while pages < MAX_PAGES:
        extra = {"cursor": cursor} if cursor else {}
        page = await ok(
            session, "fetch_all_fx_rates", source="fbil", limit=PAGE_LIMIT, **arguments, **extra
        )
        dates += [row["date"] for row in page["rates"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    expected = session.oracle.data(
        "/v1/fx/rates",
        currency="USD",
        **{"from": "2026-09-01"},
        to="2026-09-24",
        source="fbil",
        limit="1000",
    )
    require(pages >= 2, f"expected at least 2 pages, got {pages}")
    require(len(dates) == len(set(dates)), "a date appears on two pages")
    same(dates, [row["date"] for row in expected], "paged dates")
    require(cursor is None, "page walk did not finish")


async def check_compare(session: Session) -> None:
    got = await ok(
        session,
        "compare_fx_sources",
        currency="USD",
        from_date="2026-09-01",
        to_date="2026-09-24",
    )
    expected = session.oracle.data(
        "/v1/fx/compare", currency="USD", **{"from": "2026-09-01"}, to="2026-09-24"
    )
    same(got["summary"], expected["summary"], "comparison summary")
    require(got["summary"]["overlap_days"] >= 1, "RBI and FBIL share no day")


async def check_mibor(session: Session) -> None:
    got = await ok(session, "fetch_mibor", from_date="2026-09-01", to_date="2026-09-30")
    expected = session.oracle.data("/v1/rates/mibor", **{"from": "2026-09-01"}, to="2026-09-30")
    same(got["rates"], expected, "MIBOR rows")
    friday = [row for row in got["rates"] if row["tenor"] == "3D"]
    require(friday, "no 3D tenor row")
    require(all(row["spans_weekend"] for row in friday), "a 3D row does not span the weekend")


async def check_health(session: Session) -> None:
    got = await ok(session, "fetch_source_health")
    expected = session.oracle.data("/v1/sources/health")

    def key(rows: Sequence[dict[str, Any]]) -> list[tuple[str, str, str]]:
        return sorted((r["source"], r["dataset"], r["status"]) for r in rows)

    same(got["status"], expected["status"], "overall status")
    same(key(got["sources"]), key(expected["sources"]), "per-dataset status")
    require(len(got["sources"]) == 5, f"{len(got['sources'])} datasets, expected 5")


async def check_unknown_office(session: Session) -> None:
    body = await fails(session, "fetch_holidays", office="atlantis", year=2026)
    same(
        body["code"],
        session.oracle.error_code("/v1/holidays", office="atlantis", year="2026"),
        "code",
    )
    require(body["code"] == "OFFICE_NOT_FOUND", f"code is {body['code']}")
    require("fetch_all_offices" in body["hint"], f"hint is {body['hint']!r}")


async def check_missing_year(session: Session) -> None:
    body = await fails(session, "fetch_holidays", office="mumbai", year=2010)
    same(
        body["code"],
        session.oracle.error_code("/v1/holidays", office="mumbai", year="2010"),
        "code",
    )
    require(body["code"] == "CALENDAR_DATA_MISSING", f"code is {body['code']}")


async def check_bad_date(session: Session) -> None:
    body = await fails(session, "check_business_day", date="2026-3-28", office="mumbai")
    same(
        body["code"],
        session.oracle.error_code("/v1/calendar/business-day", date="2026-3-28", office="mumbai"),
        "code",
    )
    require(body["code"] == "INVALID_REQUEST", f"code is {body['code']}")


async def check_toolsets(session: Session) -> None:
    async with connect(session.db_path, ("--toolsets", "calendar")) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
    require(names == CALENDAR_TOOLS, f"calendar toolset exposes {sorted(names)}")
    require(not names & FX_TOOLS, f"fx tools leaked: {sorted(names & FX_TOOLS)}")


Check = Callable[[Session], Awaitable[None]]
# (name, tool, check). Order reads like a merchant conversation.
CHECKS: tuple[tuple[str, str, Check], ...] = (
    ("Server lists 13 tools", "tools/list", check_tools_listed),
    ("Every tool is read-only", "tools/list", check_read_only),
    ("Offices and health resources exist", "resources/read", check_resources),
    ("Settlement prompt is offered", "prompts/get", check_prompt),
    ("Which RBI offices exist?", "fetch_all_offices", check_offices),
    ("Mumbai bank holidays in March 2026", "fetch_holidays", check_holidays),
    ("Is 31 Mar 2026 a working day in Mumbai?", "check_business_day", check_business_day),
    ("Next 3 working days after 27 Mar", "fetch_next_business_days", check_next_days),
    ("Paid 27 Mar 11:00 IST: when does it settle?", "estimate_settlement_date", check_settlement),
    ("USD rate on Ganesh Chaturthi (14 Sep)", "fetch_fx_rate", check_fx_fallback),
    ("JPY 1000 in INR (rate per 100 JPY)", "convert_currency", check_convert_jpy),
    ("Same amount as a JSON number", "convert_currency", check_convert_number),
    ("Quote a USD 1200 invoice", "quote_invoice", check_quote),
    ("Page the USD series with next_cursor", "fetch_all_fx_rates", check_paging),
    ("Do RBI and FBIL agree on USD?", "compare_fx_sources", check_compare),
    ("MIBOR 3D tenor spans the weekend", "fetch_mibor", check_mibor),
    ("How fresh is the data?", "fetch_source_health", check_health),
    ("Unknown office gives OFFICE_NOT_FOUND + hint", "fetch_holidays", check_unknown_office),
    ("Year 2010 gives CALENDAR_DATA_MISSING", "fetch_holidays", check_missing_year),
    ("Bad date gives INVALID_REQUEST", "check_business_day", check_bad_date),
    ("--toolsets calendar hides the fx tools", "tools/list", check_toolsets),
)


# --------------------------------------------------------------------------- runner
def server_params(db_path: Path, extra_args: Sequence[str] = ()) -> StdioServerParameters:
    env = {
        **os.environ,
        "IMDA_DB_PATH": str(db_path),
        "IMDA_MCP_FIXED_NOW": FIXED_NOW_TEXT,
        "IMDA_ADMIN_TOKEN": "",
        "IMDA_MCP_TOKEN": "",
    }
    return StdioServerParameters(
        command=SERVER_COMMAND, args=[*SERVER_ARGS, *extra_args], env=env, cwd=str(REPO_ROOT)
    )


def connect(db_path: Path, extra_args: Sequence[str] = ()) -> Client:
    return Client(server_params(db_path, extra_args))


async def run_check(n: int, spec: tuple[str, str, Check], session: Session) -> Result:
    name, tool, check = spec
    started = time.monotonic()
    failures: list[str] = []
    try:
        await check(session)
    except CheckFailed as error:
        failures.append(str(error))
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")
    ms = round((time.monotonic() - started) * 1000)
    return Result(n, name, tool, "FAIL" if failures else "PASS", ms, failures)


async def run_checks(db_path: Path) -> list[Result]:
    """Run every check against a server on ``db_path``. A server that will not start fails all."""
    logging.getLogger("imda.api.access").disabled = True
    settings = Settings(db_path=db_path, _env_file=None)  # type: ignore[call-arg]
    results: list[Result] = []
    try:
        with TestClient(create_app(settings, now=lambda: FIXED_NOW)) as rest:
            async with connect(db_path) as client:
                session = Session(client, Oracle(rest), db_path)
                for n, spec in enumerate(CHECKS, start=1):
                    results.append(await run_check(n, spec, session))
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        done = len(results)
        results += [
            Result(n, name, tool, "FAIL", 0, [f"server did not run: {detail}"])
            for n, (name, tool, _) in enumerate(CHECKS[done:], start=done + 1)
        ] or [Result(done + 1, "Server start/stop", "-", "FAIL", 0, [detail])]
    return results


def summarize(results: Sequence[Result]) -> dict[str, int]:
    passed = sum(r.result == "PASS" for r in results)
    return {"total": len(results), "passed": passed, "failed": len(results) - passed}


def summary_line(results: Sequence[Result]) -> str:
    s = summarize(results)
    return f"{s['passed']}/{s['total']} checks passed"


def exit_code(results: Sequence[Result]) -> int:
    return 0 if results and all(r.result == "PASS" for r in results) else 1


def render_table(results: Sequence[Result]) -> str:
    rows = [(str(r.n), r.name, r.tool, r.result, str(r.ms)) for r in results]
    header = ("#", "CHECK", "TOOL", "RESULT", "ms")
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]

    def line(row: Sequence[str]) -> str:
        cells = [
            cell.rjust(widths[i]) if i in (0, 4) else cell.ljust(widths[i])
            for i, cell in enumerate(row)
        ]
        return "  ".join(cells).rstrip()

    out = [line(header), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in rows]
    for r in results:
        out += [f"  FAIL #{r.n} {r.name}"] * bool(r.failures)
        out += [f"       - {failure}" for failure in r.failures]
    return "\n".join(out)


def write_report(path: Path, results: Sequence[Result]) -> None:
    report = {
        "fixed_now": FIXED_NOW_TEXT,
        "summary": summarize(results),
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the keyless MCP demo (no API key needed).")
    parser.add_argument(
        "--db", type=Path, help="use this SQLite DB as it is (default: seed a temp one)"
    )
    parser.add_argument("--json", type=Path, metavar="FILE", help="write a JSON report")
    args = parser.parse_args(argv)

    if args.db:
        results = asyncio.run(run_checks(args.db))
    else:
        with tempfile.TemporaryDirectory(prefix="imda-demo-mcp-") as tmp:
            db_path = Path(tmp) / "imda.sqlite3"
            seed(db_path)
            results = asyncio.run(run_checks(db_path))

    print(render_table(results))
    print()
    print(summary_line(results))
    if args.json:
        write_report(args.json, results)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
