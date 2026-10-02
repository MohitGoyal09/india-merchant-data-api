"""Layer-2 agent evals: 16 merchant questions answered by Claude through the MCP server.

uv run python evals/run_agent_evals.py --dry-run          # no Claude call: validate the case file
uv run python evals/run_agent_evals.py                    # run all cases (needs credentials)
uv run python evals/run_agent_evals.py --filter holiday --model claude-opus-5-5 --json out.json
uv run python evals/run_agent_evals.py --bar 0.92         # share of cases that must pass

Scoring is deterministic (no LLM judge). A case passes when (a) every required tool was called and
no forbidden tool was, and (b) every check in ``expect`` holds for the final answer. The bar is a
share of cases (default 92%, rounded up to whole cases: 15 of 16). Every case has a category; the
summary prints the pass rate per category. Cases run against the recorded-fixture DB (or, for
``"db": "adversarial"``, the fixture DB plus one injected holiday) with the server clock fixed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cases import Check  # noqa: E402
from imda.agent import (  # noqa: E402
    DEFAULT_EFFORT,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    AgentRun,
    AnthropicClient,
    McpStdioBackend,
    TokenUsage,
    ToolBackend,
    build_system_prompt,
    has_credentials,
    make_client,
    run_agent,
)
from run_cases import evaluate, resolve  # noqa: E402
from seed_fixtures import seed, seed_adversarial  # noqa: E402

CASES_PATH: Final = Path(__file__).resolve().parent / "agent_cases.json"
RESULTS_DIR: Final = Path(__file__).resolve().parent / "results"

DEFAULT_BAR: Final = 0.92
DEFAULT_TOLERANCE: Final = 0.01
EXIT_BELOW_BAR: Final = 1
EXIT_NO_CREDENTIALS: Final = 2

# Claude Opus 5.5 list prices, USD per million tokens.
PRICE_INPUT: Final = 4.0
PRICE_OUTPUT: Final = 20.0
PRICE_CACHE_WRITE: Final = 5.0
PRICE_CACHE_READ: Final = 0.20

TOOL_NAMES: Final = frozenset(
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
CHECK_TYPES: Final = ("contains", "regex", "number")
CATEGORIES: Final = (
    "fx",
    "settlement",
    "calendar",
    "invoice",
    "rates",
    "comparison",
    "error_recovery",
    "out_of_scope",
    "safety",
)
DEFAULT_DB: Final = "fixture"
# Which seeder builds the DB a case runs against.
DB_SEEDERS: Final[Mapping[str, Callable[[Path], object]]] = {
    DEFAULT_DB: seed,
    "adversarial": seed_adversarial,
}
_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9_]*$")
_TRUTH_REF = re.compile(r"^(?P<index>\d+):(?P<path>.+)$")
# Digits with optional thousands (1,200) or lakh (1,15,091) separators and a decimal part.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")

CheckType = Literal["contains", "regex", "number"]


# --------------------------------------------------------------------------- case file
class CaseFileError(ValueError):
    """The case file is invalid. ``problems`` lists every issue found."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Expect:
    type: CheckType
    value: str | float
    tolerance: float = DEFAULT_TOLERANCE
    negate: bool = False
    truth: str | None = None

    def describe(self) -> str:
        prefix = "not " if self.negate else ""
        if self.type == "number":
            return f"{prefix}number {self.value} +/-{self.tolerance}"
        return f"{prefix}{self.type} {str(self.value)[:60]!r}"


@dataclass(frozen=True)
class GroundTruth:
    """One REST call on the fixture DB plus the checks its response must satisfy."""

    method: str
    path: str
    params: Mapping[str, Any] | None
    json: Mapping[str, Any] | None
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class AgentCase:
    id: str
    category: str
    question: str
    required_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    expect: tuple[Expect, ...]
    notes: str
    ground_truth: tuple[GroundTruth, ...]
    golden_answer: str
    db: str = DEFAULT_DB


@dataclass(frozen=True)
class CaseFile:
    fixed_now: str
    cases: tuple[AgentCase, ...]


def _parse_expect(raw: Any, where: str, problems: list[str]) -> Expect | None:
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be an object")
        return None
    kind = raw.get("type")
    value = raw.get("value")
    if kind not in CHECK_TYPES:
        problems.append(f"{where}: type must be one of {', '.join(CHECK_TYPES)}")
        return None
    tolerance = raw.get("tolerance", DEFAULT_TOLERANCE)
    negate = raw.get("negate", False)
    truth = raw.get("truth")
    issues: list[str] = []
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            issues.append("number value must be a number")
        if isinstance(tolerance, bool) or not isinstance(tolerance, int | float) or tolerance < 0:
            issues.append("tolerance must be a number >= 0")
    else:
        if not isinstance(value, str) or not value:
            issues.append(f"{kind} value must be a non-empty string")
        if "tolerance" in raw:
            issues.append("tolerance only applies to number checks")
    if kind == "regex" and isinstance(value, str):
        try:
            re.compile(value)
        except re.error as exc:
            issues.append(f"regex does not compile: {exc}")
    if not isinstance(negate, bool):
        issues.append("negate must be true or false")
    if truth is not None and (kind != "number" or not _TRUTH_REF.match(str(truth))):
        issues.append("truth must look like '<ground_truth index>:<json path>' on a number check")
    if issues:
        problems.extend(f"{where}: {issue}" for issue in issues)
        return None
    return Expect(kind, cast("str | float", value), float(tolerance), negate, truth)


def _parse_ground_truth(raw: Any, where: str, problems: list[str]) -> GroundTruth | None:
    request = raw.get("request") if isinstance(raw, dict) else None
    checks_raw = raw.get("checks") if isinstance(raw, dict) else None
    if not isinstance(request, dict) or not isinstance(checks_raw, list) or not checks_raw:
        problems.append(f"{where}: needs a 'request' object and a non-empty 'checks' list")
        return None
    method, path = request.get("method"), request.get("path")
    if method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/"):
        problems.append(f"{where}: request needs method GET|POST and a path starting with '/'")
        return None
    checks: list[Check] = []
    for number, item in enumerate(checks_raw):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            problems.append(f"{where}.checks[{number}]: needs 'path' and 'op'")
            return None
        if not isinstance(item.get("op"), str):
            problems.append(f"{where}.checks[{number}]: needs 'path' and 'op'")
            return None
        checks.append(Check(item["path"], item["op"], item.get("value")))
    return GroundTruth(method, path, request.get("params"), request.get("json"), tuple(checks))


def _tool_list(raw: Any, field_name: str, where: str, problems: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        problems.append(f"{where}.{field_name}: must be a list of tool names")
        return ()
    unknown = sorted(set(raw) - TOOL_NAMES)
    if unknown:
        problems.append(f"{where}.{field_name}: unknown tool(s) {', '.join(unknown)}")
    return tuple(raw)


def _parse_case(raw: Any, index: int, seen: set[str], problems: list[str]) -> AgentCase | None:
    where = f"cases[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be an object")
        return None
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not _CASE_ID.match(case_id):
        problems.append(f"{where}: id must be a lowercase slug (a-z, 0-9, _)")
        case_id = f"#{index}"
    elif case_id in seen:
        problems.append(f"{where}: duplicate id {case_id!r}")
    seen.add(case_id)
    where = f"case {case_id}"
    before = len(problems)
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        problems.append(f"{where}: question must be a non-empty string")
    category = raw.get("category")
    if category not in CATEGORIES:
        problems.append(f"{where}: category must be one of {', '.join(CATEGORIES)}")
    db = raw.get("db", DEFAULT_DB)
    if db not in DB_SEEDERS:
        problems.append(f"{where}: db must be one of {', '.join(DB_SEEDERS)}")
    required = _tool_list(raw.get("required_tools"), "required_tools", where, problems)
    if "required_tools" not in raw:
        problems.append(f"{where}: required_tools is missing (use [] for none)")
    forbidden = _tool_list(raw.get("forbidden_tools"), "forbidden_tools", where, problems)
    if set(required) & set(forbidden):
        problems.append(f"{where}: a tool cannot be both required and forbidden")
    expect_raw = raw.get("expect")
    if not isinstance(expect_raw, list) or not expect_raw:
        problems.append(f"{where}: expect must be a non-empty list of checks")
        expect_raw = []
    expects = [
        _parse_expect(item, f"{where}.expect[{n}]", problems) for n, item in enumerate(expect_raw)
    ]
    truths_raw = raw.get("ground_truth", [])
    if not isinstance(truths_raw, list):
        problems.append(f"{where}: ground_truth must be a list")
        truths_raw = []
    truths = [
        _parse_ground_truth(item, f"{where}.ground_truth[{n}]", problems)
        for n, item in enumerate(truths_raw)
    ]
    golden = raw.get("golden_answer")
    if not isinstance(golden, str) or not golden.strip():
        problems.append(f"{where}: golden_answer must be a non-empty string")
    if len(problems) > before or not isinstance(question, str) or not isinstance(golden, str):
        return None
    return AgentCase(
        id=case_id,
        category=str(category),
        db=str(db),
        question=question,
        required_tools=required,
        forbidden_tools=forbidden,
        expect=tuple(e for e in expects if e is not None),
        notes=str(raw.get("notes", "")),
        ground_truth=tuple(t for t in truths if t is not None),
        golden_answer=golden,
    )


def _check_truth_refs(case: AgentCase, problems: list[str]) -> None:
    for expect in case.expect:
        match = _TRUTH_REF.match(expect.truth or "")
        if match and int(match["index"]) >= len(case.ground_truth):
            problems.append(f"case {case.id}: truth {expect.truth!r} points past ground_truth")


def parse_case_file(data: Any) -> CaseFile:
    """Validate the parsed JSON. Raises ``CaseFileError`` listing every problem."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise CaseFileError(["top level must be an object"])
    fixed_now = data.get("fixed_now")
    try:
        if not isinstance(fixed_now, str) or dt.datetime.fromisoformat(fixed_now).tzinfo is None:
            raise ValueError
    except ValueError:
        problems.append("fixed_now must be an ISO timestamp with a UTC offset")
        fixed_now = ""
    cases_raw = data.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise CaseFileError([*problems, "cases must be a non-empty list"])
    seen: set[str] = set()
    cases = [_parse_case(raw, n, seen, problems) for n, raw in enumerate(cases_raw)]
    parsed = tuple(case for case in cases if case is not None)
    for case in parsed:
        _check_truth_refs(case, problems)
    if problems:
        raise CaseFileError(problems)
    return CaseFile(fixed_now=str(fixed_now), cases=parsed)


def load_case_file(path: Path = CASES_PATH) -> CaseFile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseFileError([f"cannot read {path}: {exc}"]) from exc
    return parse_case_file(data)


# --------------------------------------------------------------------------- scoring
def extract_numbers(text: str) -> list[Decimal]:
    """Every number in ``text``; thousands and lakh commas are removed (``1,15,091.88``)."""
    return [Decimal(match.replace(",", "")) for match in _NUMBER.findall(text)]


def _number_found(expect: Expect, text: str) -> bool:
    target, tolerance = Decimal(str(expect.value)), Decimal(str(expect.tolerance))
    return any(abs(found - target) <= tolerance for found in extract_numbers(text))


def check_holds(expect: Expect, text: str) -> bool:
    """Whether one check passes for ``text``. ``negate`` flips the result."""
    if expect.type == "contains":
        found = str(expect.value).lower() in text.lower()
    elif expect.type == "regex":
        found = re.search(str(expect.value), text, re.IGNORECASE) is not None
    else:
        found = _number_found(expect, text)
    return found != expect.negate


@dataclass(frozen=True)
class CaseScore:
    missing_tools: tuple[str, ...]
    forbidden_called: tuple[str, ...]
    failed_checks: tuple[str, ...]

    @property
    def tools_ok(self) -> bool:
        return not self.missing_tools and not self.forbidden_called

    @property
    def facts_ok(self) -> bool:
        return not self.failed_checks

    @property
    def passed(self) -> bool:
        return self.tools_ok and self.facts_ok


def score_answer(case: AgentCase, tools_called: Sequence[str], answer: str) -> CaseScore:
    """Score one answer. Deterministic: tool names and checks only."""
    called = set(tools_called)
    return CaseScore(
        missing_tools=tuple(name for name in case.required_tools if name not in called),
        forbidden_called=tuple(name for name in case.forbidden_tools if name in called),
        failed_checks=tuple(e.describe() for e in case.expect if not check_holds(e, answer)),
    )


# --------------------------------------------------------------------------- results
@dataclass(frozen=True)
class CaseResult:
    case: AgentCase
    run: AgentRun
    score: CaseScore

    @property
    def passed(self) -> bool:
        return self.run.completed and self.score.passed

    def failures(self) -> list[str]:
        out: list[str] = []
        if not self.run.completed:
            out.append(self.run.error or f"run ended with {self.run.stop_reason}")
        out += [f"required tool not called: {name}" for name in self.score.missing_tools]
        out += [f"forbidden tool called: {name}" for name in self.score.forbidden_called]
        out += [f"answer check failed: {text}" for text in self.score.failed_checks]
        return out

    def to_json(self) -> dict[str, Any]:
        run = self.run
        return {
            "id": self.case.id,
            "category": self.case.category,
            "db": self.case.db,
            "question": self.case.question,
            "passed": self.passed,
            "tools_ok": self.score.tools_ok,
            "facts_ok": self.score.facts_ok,
            "failures": self.failures(),
            "tool_calls": [
                {
                    "name": call.name,
                    "args": call.args,
                    "is_error": call.is_error,
                    "duration_ms": call.duration_ms,
                }
                for call in run.tool_calls
            ],
            "turns": run.turns,
            "stop_reason": run.stop_reason,
            "model_served": run.model_served,
            "fallback_used": run.fallback_used,
            "refusal": None
            if run.refusal is None
            else {"category": run.refusal.category, "explanation": run.refusal.explanation},
            "usage": {
                "input_tokens": run.usage.input_tokens,
                "output_tokens": run.usage.output_tokens,
                "cache_creation_input_tokens": run.usage.cache_creation_input_tokens,
                "cache_read_input_tokens": run.usage.cache_read_input_tokens,
            },
            "answer": run.final_text,
        }


def estimate_cost(usage: TokenUsage) -> float:
    """USD at Claude Opus 5.5 list prices. A fallback model would bill at its own rates."""
    return (
        usage.input_tokens * PRICE_INPUT
        + usage.output_tokens * PRICE_OUTPUT
        + usage.cache_creation_input_tokens * PRICE_CACHE_WRITE
        + usage.cache_read_input_tokens * PRICE_CACHE_READ
    ) / 1_000_000


def required_passes(total: int, bar: float) -> int:
    """Cases that must pass to meet ``bar`` (0 to 1): bar x total, rounded up."""
    return math.ceil(Decimal(str(bar)) * total)


def _category_summary(results: Sequence[CaseResult]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    names = [c for c in CATEGORIES if any(r.case.category == c for r in results)]
    names += sorted({r.case.category for r in results} - set(CATEGORIES))
    for name in names:
        group = [r for r in results if r.case.category == name]
        passed = sum(r.passed for r in group)
        out[name] = {"total": len(group), "passed": passed, "pass_rate": passed / len(group)}
    return out


def summarize(results: Sequence[CaseResult], bar: float = DEFAULT_BAR) -> dict[str, Any]:
    total = len(results)
    passed = sum(r.passed for r in results)
    needed = required_passes(total, bar)
    usage = sum((r.run.usage for r in results), TokenUsage())
    return {
        "total": total,
        "passed": passed,
        "pass_rate": passed / total if total else 0.0,
        "bar": f"{needed}/{total}",
        "bar_share": bar,
        "meets_bar": total > 0 and passed >= needed,
        "categories": _category_summary(results),
        "tool_selection_accuracy": sum(r.score.tools_ok for r in results) / total if total else 0.0,
        "avg_tool_calls": sum(len(r.run.tool_calls) for r in results) / total if total else 0.0,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "estimated_cost_usd": round(estimate_cost(usage), 4),
    }


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int]) -> list[str]:
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]

    def line(row: Sequence[str]) -> str:
        cells = [
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
            for i, cell in enumerate(row)
        ]
        return "  ".join(cells).rstrip()

    return [line(header), "  ".join("-" * w for w in widths), *(line(row) for row in rows)]


def render_results(results: Sequence[CaseResult]) -> str:
    rows = [
        (
            str(n),
            r.case.id,
            "✓" if r.score.tools_ok else "✗",
            "✓" if r.score.facts_ok and r.run.completed else "✗",
            str(r.run.turns),
            str(r.run.usage.total),
            "PASS" if r.passed else "FAIL",
        )
        for n, r in enumerate(results, start=1)
    ]
    header = ("#", "case", "tools ✓", "facts ✓", "turns", "tokens", "result")
    out = _table(header, rows, right=(0, 4, 5))
    for n, r in enumerate(results, start=1):
        if not r.passed:
            out.append(f"  FAIL #{n} {r.case.id}")
            out += [f"       - {failure}" for failure in r.failures()]
    return "\n".join(out)


def render_summary(summary: Mapping[str, Any]) -> str:
    verdict = "MEETS BAR" if summary["meets_bar"] else "BELOW BAR"
    lines = [
        f"pass rate           {summary['passed']}/{summary['total']} "
        f"({summary['pass_rate']:.0%}); bar {summary['bar']} "
        f"(>= {summary['bar_share']:.0%}): {verdict}",
    ]
    for name, stats in summary["categories"].items():
        lines.append(f"  {name:<18}{stats['passed']}/{stats['total']} ({stats['pass_rate']:.0%})")
    lines += [
        f"tool selection      {summary['tool_selection_accuracy']:.0%}",
        f"avg tool calls      {summary['avg_tool_calls']:.1f}",
        f"tokens              {summary['input_tokens']} input, "
        f"{summary['output_tokens']} output "
        f"(+{summary['cache_creation_input_tokens']} cache write, "
        f"{summary['cache_read_input_tokens']} cache read)",
        f"estimated cost      ${summary['estimated_cost_usd']:.2f} at ${PRICE_INPUT:.0f}/"
        f"${PRICE_OUTPUT:.0f} per MTok",
    ]
    return "\n".join(lines)


def results_document(
    results: Sequence[CaseResult],
    *,
    model: str,
    effort: str,
    fallback: bool,
    fixed_now: str,
    bar: float = DEFAULT_BAR,
) -> dict[str, Any]:
    return {
        "timestamp": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "model": model,
        "effort": effort,
        "fallback": fallback,
        "fixed_now": fixed_now,
        "summary": summarize(results, bar),
        "cases": [r.to_json() for r in results],
    }


def write_results(document: Mapping[str, Any], directory: Path, extra: Path | None) -> list[Path]:
    """Write the timestamped file (and ``extra`` when given). Returns the paths written."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    written = [directory / f"{stamp}.json"]
    if extra is not None:
        written.append(extra)
    for path in written:
        path.write_text(text, encoding="utf-8")
    return written


# --------------------------------------------------------------------------- ground truth
def _truth_problems(case: AgentCase, responses: Sequence[Any]) -> list[str]:
    problems: list[str] = []
    for index, (truth, response) in enumerate(zip(case.ground_truth, responses, strict=True)):
        problems += [
            f"{case.id}: ground truth #{index}: {f}" for f in evaluate(response, truth.checks)
        ]
    for expect in case.expect:
        match = _TRUTH_REF.match(expect.truth or "")
        if not match:
            continue
        actual = resolve(responses[int(match["index"])], match["path"])
        try:
            drift = abs(Decimal(str(actual)) - Decimal(str(expect.value)))
        except InvalidOperation:
            problems.append(f"{case.id}: truth {expect.truth} is not a number: {actual!r}")
            continue
        if drift > Decimal(str(expect.tolerance)):
            problems.append(
                f"{case.id}: expected {expect.value} but {expect.truth} is {actual} "
                f"(tolerance {expect.tolerance})"
            )
    return problems


def _truth_problems_for_db(kind: str, cases: Sequence[AgentCase], now: dt.datetime) -> list[str]:
    """Run each case's ground-truth requests on a DB seeded for ``kind``."""
    from fastapi.testclient import TestClient

    from imda.api.app import create_app
    from imda.config import Settings

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="imda-agent-evals-") as tmp:
        db_path = Path(tmp) / "imda.sqlite3"
        DB_SEEDERS[kind](db_path)
        app = create_app(Settings(db_path=db_path, _env_file=None), now=lambda: now)
        logging.getLogger("imda.api.access").disabled = True
        with TestClient(app) as client:
            for case in cases:
                responses = [
                    client.request(t.method, t.path, params=t.params, json=t.json)
                    for t in case.ground_truth
                ]
                problems += _truth_problems(case, responses)
    return problems


def verify_case_file(case_file: CaseFile) -> list[str]:
    """Check expected values against the DB each case runs on (REST layer) and every golden
    answer against its own checks. Returns problems; empty means the file is sound."""
    now = dt.datetime.fromisoformat(case_file.fixed_now)
    problems: list[str] = []
    for kind in DB_SEEDERS:
        cases = [c for c in case_file.cases if c.db == kind]
        if cases:
            problems += _truth_problems_for_db(kind, cases, now)
    for case in case_file.cases:
        failed = score_answer(case, case.required_tools, case.golden_answer).failed_checks
        problems += [f"{case.id}: golden answer fails check: {text}" for text in failed]
    return problems


def render_dry_run(case_file: CaseFile, problems: Sequence[str]) -> str:
    bad = {p.split(":", 1)[0] for p in problems}
    rows = [
        (
            str(n),
            case.id,
            case.category,
            case.db,
            ", ".join(case.required_tools) or "-",
            str(len(case.expect)),
            str(len(case.ground_truth)),
            "FAIL" if case.id in bad else "ok",
        )
        for n, case in enumerate(case_file.cases, start=1)
    ]
    header = ("#", "case", "category", "db", "required tools", "checks", "truth calls", "verified")
    out = _table(header, rows, right=(0, 5, 6))
    out += [f"  - {problem}" for problem in problems]
    return "\n".join(out)


# --------------------------------------------------------------------------- running
async def run_cases(
    cases: Sequence[AgentCase],
    backend: ToolBackend,
    client: AnthropicClient,
    *,
    model: str,
    effort: str,
    fallback: bool,
    max_turns: int,
    system: str,
    on_result: Callable[[int, int, CaseResult], None] | None = None,
) -> list[CaseResult]:
    """Run each case in turn against one shared backend."""
    results: list[CaseResult] = []
    for number, case in enumerate(cases, start=1):
        run = await run_agent(
            case.question,
            backend,
            client,
            model=model,
            effort=effort,
            fallback=fallback,
            max_turns=max_turns,
            system=system,
        )
        score = score_answer(case, [call.name for call in run.tool_calls], run.final_text)
        result = CaseResult(case, run, score)
        results.append(result)
        if on_result is not None:
            on_result(number, len(cases), result)
    return results


BackendFactory = Callable[[Path, str], AbstractAsyncContextManager[ToolBackend]]


def default_backend(db_path: Path, fixed_now: str) -> AbstractAsyncContextManager[ToolBackend]:
    return McpStdioBackend(
        db_path=db_path, fixed_now=fixed_now, extra_env={"IMDA_UPSTREAM_ENABLED": "false"}
    )


def _progress(number: int, total: int, result: CaseResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    print(f"[{number}/{total}] {result.case.id}: {status}", file=sys.stderr, flush=True)


async def _run_all(
    case_file: CaseFile,
    cases: Sequence[AgentCase],
    client: AnthropicClient,
    backend_factory: BackendFactory,
    args: argparse.Namespace,
) -> list[CaseResult]:
    """Run ``cases`` with one seeded DB and one backend per DB kind they use.

    Results come back in the order of ``cases``, whatever the DB mix.
    """
    done = 0

    def progress(_number: int, _total: int, result: CaseResult) -> None:
        nonlocal done
        done += 1
        _progress(done, len(cases), result)

    by_id: dict[str, CaseResult] = {}
    with tempfile.TemporaryDirectory(prefix="imda-agent-evals-") as tmp:
        for kind in DB_SEEDERS:
            group = [c for c in cases if c.db == kind]
            if not group:
                continue
            db_path = Path(tmp) / f"{kind}.sqlite3"
            DB_SEEDERS[kind](db_path)
            async with backend_factory(db_path, case_file.fixed_now) as backend:
                results = await run_cases(
                    group,
                    backend,
                    client,
                    model=args.model,
                    effort=args.effort,
                    fallback=not args.no_fallback,
                    max_turns=args.max_turns,
                    system=build_system_prompt(case_file.fixed_now),
                    on_result=progress,
                )
            by_id.update({r.case.id: r for r in results})
    return [by_id[c.id] for c in cases]


def _bar_share(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1 (for example 0.92)")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the layer-2 agent evals.")
    parser.add_argument("--filter", metavar="TEXT", help="only cases whose id/question has TEXT")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--effort", default=DEFAULT_EFFORT, choices=("low", "medium", "high", "xhigh", "max")
    )
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--no-fallback", action="store_true", help="turn off refusal fallback")
    parser.add_argument("--json", type=Path, metavar="FILE", help="also write the results here")
    parser.add_argument("--dry-run", action="store_true", help="validate cases; no Claude call")
    parser.add_argument(
        "--bar",
        type=_bar_share,
        default=DEFAULT_BAR,
        metavar="SHARE",
        help="share of cases that must pass, 0 to 1 (default 0.92: 15 of 16)",
    )
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="case file to use")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], AnthropicClient] = make_client,
    backend_factory: BackendFactory = default_backend,
) -> int:
    args = _parser().parse_args(argv)
    try:
        case_file = load_case_file(args.cases)
    except CaseFileError as exc:
        print(
            f"invalid case file {args.cases}:\n  - " + "\n  - ".join(exc.problems), file=sys.stderr
        )
        return EXIT_BELOW_BAR

    if args.dry_run:
        problems = verify_case_file(case_file)
        print(render_dry_run(case_file, problems))
        print()
        print(f"{len(case_file.cases)} cases: {'invalid' if problems else 'valid'}")
        return EXIT_BELOW_BAR if problems else 0

    needle = (args.filter or "").lower()
    cases = [c for c in case_file.cases if needle in f"{c.id} {c.question}".lower()]
    if not cases:
        print(f"no case matches --filter {args.filter!r}", file=sys.stderr)
        return EXIT_BELOW_BAR
    client = client_factory()
    if not has_credentials(client):
        print(
            "no Claude credentials found: set ANTHROPIC_API_KEY or run `ant auth login`",
            file=sys.stderr,
        )
        return EXIT_NO_CREDENTIALS

    results = asyncio.run(_run_all(case_file, cases, client, backend_factory, args))
    document = results_document(
        results,
        model=args.model,
        effort=args.effort,
        fallback=not args.no_fallback,
        fixed_now=case_file.fixed_now,
        bar=args.bar,
    )
    paths = write_results(document, args.results_dir, args.json)
    print(render_results(results))
    print()
    print(render_summary(document["summary"]))
    print("results: " + ", ".join(str(p) for p in paths))
    return 0 if document["summary"]["meets_bar"] else EXIT_BELOW_BAR


__all__ = [
    "AgentCase",
    "CaseFile",
    "CaseFileError",
    "Expect",
    "check_holds",
    "extract_numbers",
    "load_case_file",
    "main",
    "parse_case_file",
    "required_passes",
    "score_answer",
]

if __name__ == "__main__":
    sys.exit(main())
