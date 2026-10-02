"""The keyless MCP demo: a full green run, exit-code semantics, the table and the JSON report."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"
RUN_TIMEOUT_SECONDS = 180

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")


@pytest.fixture(scope="module")
def demo() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import demo_mcp as module
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "demo_mcp.py"), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture(scope="module")
def green_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    report = tmp_path_factory.mktemp("demo-mcp") / "report.json"
    return _run("--json", str(report)), report


@needs_uv
def test_demo_passes_every_check_and_exits_zero(
    green_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    proc, _ = green_run

    summary = re.search(r"^(\d+)/(\d+) checks passed$", proc.stdout, re.MULTILINE)
    assert summary, proc.stdout
    assert summary.group(1) == summary.group(2)
    assert int(summary.group(2)) >= 20
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout


@needs_uv
def test_demo_prints_the_documented_table_columns(
    green_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    proc, _ = green_run

    header = proc.stdout.splitlines()[0].split()
    assert header == ["#", "CHECK", "TOOL", "RESULT", "ms"]


@needs_uv
def test_demo_writes_a_json_report(
    green_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    _, report_path = green_run

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["fixed_now"] == "2026-09-30T15:00:00+05:30"
    assert report["summary"]["failed"] == 0
    assert report["summary"]["passed"] == report["summary"]["total"] == len(report["results"])
    assert {r["result"] for r in report["results"]} == {"PASS"}


@needs_uv
def test_demo_exits_one_when_the_database_has_no_data(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite3"
    empty.touch()

    proc = _run("--db", str(empty))

    assert proc.returncode == 1
    summary = re.search(r"^(\d+)/(\d+) checks passed$", proc.stdout, re.MULTILINE)
    assert summary, proc.stdout
    assert int(summary.group(1)) < int(summary.group(2))
    assert "FAIL #" in proc.stdout


def test_exit_code_is_zero_only_when_all_results_pass(demo: ModuleType) -> None:
    passed = demo.Result(1, "a", "t", "PASS", 1)
    failed = demo.Result(2, "b", "t", "FAIL", 1, ["boom"])

    assert demo.exit_code([passed]) == 0
    assert demo.exit_code([passed, failed]) == 1
    assert demo.exit_code([]) == 1


def test_summary_line_counts_passes(demo: ModuleType) -> None:
    results = [
        demo.Result(1, "a", "t", "PASS", 1),
        demo.Result(2, "b", "t", "FAIL", 1, ["boom"]),
    ]

    assert demo.summary_line(results) == "1/2 checks passed"
    table = demo.render_table(results)
    assert "FAIL #2 b" in table
    assert "- boom" in table


def test_every_check_has_a_unique_name_and_the_catalogue_is_big_enough(demo: ModuleType) -> None:
    names = [name for name, _, _ in demo.CHECKS]

    assert len(names) == len(set(names))
    assert len(names) >= 20
