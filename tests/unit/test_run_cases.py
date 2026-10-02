"""The offline case runner: seeding, a full green run, failure reporting and the JSON report."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"


@pytest.fixture(scope="module")
def run_cases() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import run_cases as module
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


@pytest.fixture(scope="module")
def seed_fixtures() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import seed_fixtures as module
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def _dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Data tables' rows without the per-run id / timestamp columns, for determinism checks."""
    conn = sqlite3.connect(path)
    try:
        dump: dict[str, list[tuple[object, ...]]] = {}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            if not table.startswith(("holiday", "fx_rate", "mibor", "office")):
                continue
            columns = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})")
                if not (row[1].endswith(("_at", "fetch_id")) or row[1] == "id")
            ]
            rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            dump[table] = sorted(rows, key=repr)
        return dump
    finally:
        conn.close()


def test_seed_builds_the_expected_data_set(seed_fixtures: ModuleType, tmp_path: Path) -> None:
    summary = seed_fixtures.seed(tmp_path / "a.sqlite3")

    assert summary.offices == 34
    assert summary.holidays > 0
    assert summary.fx_rates > 0
    assert summary.mibor_rates > 0
    assert summary.health_rows == 5


def test_seed_is_deterministic(seed_fixtures: ModuleType, tmp_path: Path) -> None:
    first = seed_fixtures.seed(tmp_path / "a.sqlite3")
    second = seed_fixtures.seed(tmp_path / "b.sqlite3")

    assert first == second
    assert _dump(tmp_path / "a.sqlite3") == _dump(tmp_path / "b.sqlite3")


def test_offline_run_passes_every_case(
    run_cases: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_cases.main(["--offline"])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "FAIL" not in out
    assert out.count("PASS") >= 25
    assert "CASE" in out
    assert "failed" in out.splitlines()[-1]


def test_a_broken_case_fails_and_exits_one(
    run_cases: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = run_cases.Case(
        name="deliberately wrong",
        method="GET",
        path="/healthz",
        expect="200 status=nope",
        checks=(run_cases.Check("status", "eq", "nope"),),
    )

    code = run_cases.main(["--offline"], cases=(broken,))

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "deliberately wrong" in out
    assert "1 failed" in out


def test_filter_selects_cases_by_substring(
    run_cases: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_cases.main(["--offline", "--filter", "healthz"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.count("PASS") == 1
    assert "healthz" in out


def test_json_report_shape(run_cases: ModuleType, tmp_path: Path) -> None:
    report = tmp_path / "report.json"

    code = run_cases.main(["--offline", "--json", str(report)])

    parsed = json.loads(report.read_text(encoding="utf-8"))
    assert code == 0
    assert parsed["mode"] == "offline"
    assert set(parsed["summary"]) == {"total", "passed", "failed", "skipped"}
    assert parsed["summary"]["failed"] == 0
    assert parsed["summary"]["total"] == len(parsed["results"])
    first = parsed["results"][0]
    assert set(first) == {"n", "name", "method", "path", "expect", "result", "ms", "failures"}
    assert first["result"] == "PASS"
    assert first["failures"] == []


def test_live_mode_skips_offline_only_cases(run_cases: ModuleType) -> None:
    offline_only = [c for c in run_cases.CASES if c.scope == "offline"]
    live_only = [c for c in run_cases.CASES if c.scope == "live"]

    assert offline_only, "at least one case asserts exact fixture values"
    assert live_only, "at least one case asserts a live-only stable fact"
    assert all(c.runs_in("live") is False for c in offline_only)
    assert all(c.runs_in("offline") is False for c in live_only)


def test_unreachable_base_url_fails_without_crashing(
    run_cases: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_cases.main(["--base-url", "http://127.0.0.1:9", "--filter", "healthz"])

    assert code == 1
    assert "FAIL" in capsys.readouterr().out
