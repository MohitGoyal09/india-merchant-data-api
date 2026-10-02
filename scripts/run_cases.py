"""Run the named API cases and print a PASS / FAIL / SKIP table. Exit code 1 on any FAIL.

uv run python scripts/run_cases.py --offline             # fixture DB, in-process (default)
uv run python scripts/run_cases.py --base-url http://127.0.0.1:8000   # a running `imda serve`
uv run python scripts/run_cases.py --offline --json report.json --filter settlement
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES, Case, Check, Mode, Send
from seed_fixtures import FIXTURE_NOW, seed

__all__ = ["CASES", "Case", "Check", "main"]

Outcome = Literal["PASS", "FAIL", "SKIP"]
TIMEOUT_SECONDS = 30.0
_MISSING: Any = object()
_FILTER = re.compile(r"^\{(?P<key>[^=}]+)=(?P<value>.+)\}$")
_TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "bool": bool,
    "list": list,
    "dict": dict,
}


@dataclass(frozen=True)
class Result:
    n: int
    name: str
    method: str
    path: str
    expect: str
    result: Outcome
    ms: int
    failures: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ path + ops
def _step(node: Any, segment: str) -> Any:
    if node is _MISSING:
        return _MISSING
    picked = _FILTER.match(segment)
    if picked and isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and str(item.get(picked["key"])) == picked["value"]:
                return item
        return _MISSING
    if isinstance(node, list) and segment.isdigit():
        index = int(segment)
        return node[index] if index < len(node) else _MISSING
    if isinstance(node, dict):
        return node.get(segment, _MISSING)
    return _MISSING


def resolve(response: httpx.Response, path: str) -> Any:
    head, _, rest = path.partition(".")
    if head == "@status":
        return response.status_code
    if head == "@text":
        return response.text
    if head == "@lines":
        node: Any = response.text.splitlines()
    elif head == "@header":
        return response.headers.get(rest, _MISSING)
    else:
        try:
            node = response.json()
        except ValueError:
            return _MISSING
        rest = path
    for segment in filter(None, rest.split(".")):
        node = _step(node, segment)
    return node


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _num(op: str, actual: Any, expected: Any) -> bool:
    left, right = _as_decimal(actual), _as_decimal(expected)
    if left is None or right is None:
        return False
    return {
        "num_eq": left == right,
        "ge": left >= right,
        "gt": left > right,
        "le": left <= right,
        "lt": left < right,
    }[op]


def _holds(op: str, actual: Any, expected: Any) -> bool:
    if op == "absent":
        return actual is _MISSING
    if actual is _MISSING:
        return False
    simple: dict[str, Callable[[], bool]] = {
        "exists": lambda: True,
        "eq": lambda: type(actual) is type(expected) and actual == expected,
        "ne": lambda: actual != expected,
        "contains": lambda: expected in actual,
        "startswith": lambda: isinstance(actual, str) and actual.startswith(expected),
        "endswith": lambda: isinstance(actual, str) and actual.endswith(expected),
        "matches": lambda: isinstance(actual, str) and re.search(expected, actual) is not None,
        "len_eq": lambda: len(actual) == expected,
        "len_ge": lambda: len(actual) >= expected,
        "type": lambda: type(actual) is _TYPES[expected],
        "unique": lambda: len({item[expected] for item in actual}) == len(actual),
    }
    if op in simple:
        return simple[op]()
    return _num(op, actual, expected)


def _show(value: Any) -> str:
    if value is _MISSING:
        return "<missing>"
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def evaluate(response: httpx.Response, checks: Sequence[Check]) -> list[str]:
    failures: list[str] = []
    for check in checks:
        actual = resolve(response, check.path)
        try:
            ok = _holds(check.op, actual, check.value)
        except (TypeError, KeyError, ValueError):
            ok = False
        if not ok:
            failures.append(f"{check.path} {check.op} {_show(check.value)}: got {_show(actual)}")
    return failures


# ------------------------------------------------------------------ running
def run_case(number: int, case: Case, mode: Mode, send: Send) -> Result:
    def result(outcome: Outcome, ms: int = 0, failures: list[str] | None = None) -> Result:
        return Result(
            number, case.name, case.method, case.path, case.expect, outcome, ms, failures or []
        )

    if not case.runs_in(mode):
        return result("SKIP")
    started = time.perf_counter()
    try:
        response = _send(send, case)
        checks = (*case.checks, *(case.offline_checks if mode == "offline" else ()))
        failures = evaluate(response, checks)
        if case.hook is not None and not failures:
            failures = case.hook(send, response)
    except Exception as exc:  # report any transport/parse error as a failed case, never crash
        failures = [f"{type(exc).__name__}: {exc}"]
    ms = round((time.perf_counter() - started) * 1000)
    return result("FAIL" if failures else "PASS", ms, failures)


def _send(send: Send, case: Case) -> httpx.Response:
    return send(case.method, case.path, params=case.params, json=case.json, headers=case.headers)


def run_all(
    cases: Sequence[Case], mode: Mode, send: Send, substring: str | None = None
) -> list[Result]:
    needle = (substring or "").lower()
    return [
        run_case(n, case, mode, send)
        for n, case in enumerate(cases, start=1)
        if needle in f"{case.name} {case.path}".lower()
    ]


# ------------------------------------------------------------------ output
def summarize(results: Sequence[Result]) -> dict[str, int]:
    count = {o: sum(r.result == o for r in results) for o in ("PASS", "FAIL", "SKIP")}
    return {
        "total": len(results),
        "passed": count["PASS"],
        "failed": count["FAIL"],
        "skipped": count["SKIP"],
    }


def render_table(results: Sequence[Result]) -> str:
    rows = [
        (str(r.n), r.name, f"{r.method} {r.path}", r.expect, r.result, str(r.ms)) for r in results
    ]
    header = ("#", "CASE", "ENDPOINT", "EXPECT", "RESULT", "ms")
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]

    def line(row: Sequence[str]) -> str:
        cells = [
            cell.rjust(widths[i]) if i in (0, 5) else cell.ljust(widths[i])
            for i, cell in enumerate(row)
        ]
        return "  ".join(cells).rstrip()

    out = [line(header), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in rows]
    for r in results:
        out += [f"  FAIL #{r.n} {r.name}"] * bool(r.failures)
        out += [f"       - {failure}" for failure in r.failures]
    return "\n".join(out)


def summary_line(results: Sequence[Result]) -> str:
    s = summarize(results)
    return f"{s['total']} cases: {s['passed']} passed, {s['failed']} failed, {s['skipped']} skipped"


def write_report(path: Path, mode: Mode, base_url: str | None, results: Sequence[Result]) -> None:
    report = {
        "mode": mode,
        "base_url": base_url,
        "summary": summarize(results),
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ clients
def _sender(client: httpx.Client) -> Send:
    def send(
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        return client.request(method, path, params=params, json=json, headers=headers)

    return send


def _run_offline(cases: Sequence[Case], substring: str | None) -> list[Result]:
    from fastapi.testclient import TestClient

    from imda.api.app import create_app
    from imda.config import Settings

    with tempfile.TemporaryDirectory(prefix="imda-cases-") as tmp:
        db_path = Path(tmp) / "imda.sqlite3"
        seed(db_path)
        settings = Settings(db_path=db_path, _env_file=None)
        app = create_app(settings, now=lambda: FIXTURE_NOW)
        logging.getLogger("imda.api.access").disabled = True
        with TestClient(app) as client:
            return run_all(cases, "offline", _sender(client), substring)


def _run_live(base_url: str, cases: Sequence[Case], substring: str | None) -> list[Result]:
    with httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS) as client:
        return run_all(cases, "live", _sender(client), substring)


def main(argv: Sequence[str] | None = None, cases: Sequence[Case] = CASES) -> int:
    parser = argparse.ArgumentParser(description="Run the India Merchant Data API cases.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--offline", action="store_true", help="seed a temp DB, run in-process")
    where.add_argument(
        "--base-url", help="run against a running `imda serve`, e.g. http://127.0.0.1:8000"
    )
    parser.add_argument("--json", type=Path, metavar="FILE", help="write a JSON report")
    parser.add_argument("--filter", metavar="TEXT", help="only cases whose name/path has TEXT")
    args = parser.parse_args(argv)

    mode: Mode = "live" if args.base_url else "offline"
    if mode == "live":
        results = _run_live(args.base_url, cases, args.filter)
    else:
        results = _run_offline(cases, args.filter)

    print(render_table(results))
    print()
    print(summary_line(results))
    if args.json:
        write_report(args.json, mode, args.base_url, results)
    return 1 if any(r.result == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
