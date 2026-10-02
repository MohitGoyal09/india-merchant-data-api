"""The layer-2 agent eval harness: case file, scorer, summary, dry run and the run path.

No network and no API key: the run path uses a fake Anthropic client and an in-memory backend.
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from imda.agent import AgentRun, TokenUsage, ToolCall
from tests.unit.test_agent_loop import FakeBackend, ScriptedClient, message, text, tool_use

REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "evals"
CASES_FILE = EVALS / "agent_cases.json"
EXPECTED_IDS = {
    "usd_invoice_inr_and_settlement",
    "mumbai_holiday_mar31",
    "t2_settlement_skipped_days",
    "usd_rate_holiday_fallback",
    "jpy_to_inr_per_100_unit",
    "next_3_business_days_mumbai",
    "rbi_vs_fbil_usd_sept",
    "usd_inr_september_stats",
    "mibor_friday_3d_tenor",
    "mumbai_holidays_april_2026",
    "holidays_2010_not_loaded",
    "gst_rate_out_of_scope",
}


@pytest.fixture(scope="module")
def harness() -> Iterator[ModuleType]:
    sys.path.insert(0, str(EVALS))
    try:
        yield importlib.import_module("run_agent_evals")
    finally:
        sys.path.remove(str(EVALS))


@pytest.fixture
def raw_cases() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return data


def case_from(harness: ModuleType, **overrides: Any) -> Any:
    """A parsed case: a valid minimal one with ``overrides`` applied before parsing."""
    base: dict[str, Any] = {
        "id": "demo_case",
        "question": "Q?",
        "required_tools": ["fetch_mibor"],
        "expect": [{"type": "contains", "value": "x"}],
        "golden_answer": "x",
    }
    doc = {"fixed_now": "2026-09-30T15:00:00+05:30", "cases": [{**base, **overrides}]}
    return harness.parse_case_file(doc).cases[0]


def expect(harness: ModuleType, **fields: Any) -> Any:
    return harness.Expect(**{"type": "contains", "value": "x", **fields})


# --------------------------------------------------------------------------- case file
def test_shipped_case_file_has_the_twelve_planned_cases(harness: ModuleType) -> None:
    case_file = harness.load_case_file(CASES_FILE)

    assert {c.id for c in case_file.cases} == EXPECTED_IDS
    assert len(case_file.cases) == 12
    assert case_file.fixed_now == "2026-09-30T15:00:00+05:30"
    for case in case_file.cases:
        assert case.expect, case.id
        assert case.golden_answer, case.id
        assert set(case.required_tools) <= harness.TOOL_NAMES
    gst = next(c for c in case_file.cases if c.id == "gst_rate_out_of_scope")
    assert gst.required_tools == ()


@pytest.mark.parametrize(
    ("mutate", "message_part"),
    [
        (lambda c: c.pop("question"), "question"),
        (lambda c: c.pop("required_tools"), "required_tools is missing"),
        (lambda c: c.update(required_tools=["no_such_tool"]), "unknown tool"),
        (lambda c: c.update(forbidden_tools=["fetch_mibor"]), "both required and forbidden"),
        (lambda c: c.update(expect=[]), "non-empty list of checks"),
        (lambda c: c.update(expect=[{"type": "fuzzy", "value": "x"}]), "type must be one of"),
        (lambda c: c.update(expect=[{"type": "regex", "value": "("}]), "does not compile"),
        (lambda c: c.update(expect=[{"type": "number", "value": "9"}]), "must be a number"),
        (
            lambda c: c.update(expect=[{"type": "contains", "value": "x", "tolerance": 1}]),
            "only applies to number",
        ),
        (
            lambda c: c.update(expect=[{"type": "number", "value": 1, "truth": "5:data.x"}]),
            "points past ground_truth",
        ),
        (lambda c: c.update(id="Bad Id"), "lowercase slug"),
        (lambda c: c.update(golden_answer=""), "golden_answer"),
        (lambda c: c.update(ground_truth=[{"request": {}, "checks": []}]), "ground_truth[0]"),
    ],
)
def test_bad_cases_are_rejected_with_a_clear_problem(
    harness: ModuleType, mutate: Any, message_part: str
) -> None:
    doc = {
        "fixed_now": "2026-09-30T15:00:00+05:30",
        "cases": [
            {
                "id": "demo_case",
                "question": "Q?",
                "required_tools": ["fetch_mibor"],
                "expect": [{"type": "contains", "value": "x"}],
                "golden_answer": "x",
            }
        ],
    }
    mutate(doc["cases"][0])

    with pytest.raises(harness.CaseFileError) as caught:
        harness.parse_case_file(doc)

    assert message_part in str(caught.value)


def test_duplicate_ids_and_a_naive_clock_are_rejected(
    harness: ModuleType, raw_cases: dict[str, Any]
) -> None:
    doc = copy.deepcopy(raw_cases)
    doc["cases"].append(copy.deepcopy(doc["cases"][0]))
    doc["fixed_now"] = "2026-09-30T15:00:00"

    with pytest.raises(harness.CaseFileError) as caught:
        harness.parse_case_file(doc)

    assert "duplicate id" in str(caught.value)
    assert "fixed_now" in str(caught.value)


def test_unreadable_case_file_is_a_case_file_error(harness: ModuleType, tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(harness.CaseFileError, match="cannot read"):
        harness.load_case_file(broken)


# --------------------------------------------------------------------------- scorer
def test_contains_is_case_insensitive(harness: ModuleType) -> None:
    check = expect(harness, type="contains", value="Mahavir")

    assert harness.check_holds(check, "holiday for MAHAVIR jayanti")
    assert not harness.check_holds(check, "holiday")


def test_regex_check_matches_anywhere_and_ignores_case(harness: ModuleType) -> None:
    check = expect(harness, type="regex", value=r"\b4th saturday\b")

    assert harness.check_holds(check, "28 March (4th Saturday)")
    assert not harness.check_holds(check, "14th Saturday")


@pytest.mark.parametrize(
    ("answer", "found"),
    [
        ("That is INR 1,15,091.88 in total.", True),
        ("About 115,092 rupees.", True),  # within the tolerance of 1.0
        ("INR 115091.88.", True),  # a trailing full stop is not part of the number
        ("It is 115,100 rupees.", False),
        ("The rate 95.9099 applies.", False),
        ("Settles 2026-09-28.", False),  # date parts are not the amount
    ],
)
def test_number_check_uses_a_tolerance_and_reads_indian_grouping(
    harness: ModuleType, answer: str, found: bool
) -> None:
    check = expect(harness, type="number", value=115091.88, tolerance=1.0)

    assert harness.check_holds(check, answer) is found


def test_number_default_tolerance_is_one_hundredth(harness: ModuleType) -> None:
    check = expect(harness, type="number", value=95.46)

    assert harness.check_holds(check, "average 95.47")
    assert not harness.check_holds(check, "average 95.48")


def test_negate_inverts_every_check_type(harness: ModuleType) -> None:
    assert not harness.check_holds(expect(harness, value="yes", negate=True), "Yes it is")
    assert harness.check_holds(expect(harness, type="regex", value=r"\d+%", negate=True), "no rate")
    assert not harness.check_holds(
        expect(harness, type="regex", value=r"\d+%", negate=True), "it is 5%"
    )
    assert harness.check_holds(
        expect(harness, type="number", value=60620, tolerance=1, negate=True), "606.20 rupees"
    )


def test_extract_numbers_handles_lakh_and_thousand_commas(harness: ModuleType) -> None:
    found = [str(n) for n in harness.extract_numbers("1,15,091.88 and 1,200 and 3, 4 and 95.46.")]

    assert found == ["1,15,091.88".replace(",", ""), "1200", "3", "4", "95.46"]


def test_score_requires_every_tool_and_no_forbidden_tool(harness: ModuleType) -> None:
    case = case_from(
        harness,
        required_tools=["fetch_mibor", "fetch_fx_rate"],
        forbidden_tools=["fetch_source_health"],
    )

    ok = harness.score_answer(case, ["fetch_fx_rate", "fetch_mibor"], "x")
    missing = harness.score_answer(case, ["fetch_mibor"], "x")
    forbidden = harness.score_answer(
        case, ["fetch_fx_rate", "fetch_mibor", "fetch_source_health"], "x"
    )

    assert ok.passed
    assert missing.missing_tools == ("fetch_fx_rate",)
    assert not missing.tools_ok
    assert missing.facts_ok
    assert forbidden.forbidden_called == ("fetch_source_health",)
    assert not forbidden.passed


def test_score_reports_failed_checks_without_failing_tools(harness: ModuleType) -> None:
    case = case_from(harness)

    score = harness.score_answer(case, ["fetch_mibor"], "nothing relevant")

    assert score.tools_ok
    assert not score.facts_ok
    assert len(score.failed_checks) == 1
    assert not score.passed


def test_every_golden_answer_passes_its_own_checks(harness: ModuleType) -> None:
    for case in harness.load_case_file(CASES_FILE).cases:
        score = harness.score_answer(case, case.required_tools, case.golden_answer)
        assert score.passed, (case.id, score.failed_checks)


def test_wrong_answers_fail_the_shipped_checks(harness: ModuleType) -> None:
    cases = {c.id: c for c in harness.load_case_file(CASES_FILE).cases}
    wrong = {
        "jpy_to_inr_per_100_unit": ("convert_currency", "JPY 1,000 is INR 60,620."),
        "gst_rate_out_of_scope": ("", "Cotton t-shirts attract 5% GST."),
        "holidays_2010_not_loaded": ("fetch_holidays", "Holidays: Diwali, Holi, Republic Day."),
        "usd_rate_holiday_fallback": ("fetch_fx_rate", "The rate for 14 September is 99.99."),
    }

    for case_id, (tool, answer) in wrong.items():
        called = [tool] if tool else []
        assert not harness.score_answer(cases[case_id], called, answer).passed, case_id


# --------------------------------------------------------------------------- results + summary
def _result(harness: ModuleType, case: Any, passed: bool, **usage: int) -> Any:
    run = AgentRun(
        question=case.question,
        final_text=case.golden_answer if passed else "wrong",
        tool_calls=tuple(ToolCall(t, {}, False, 5) for t in case.required_tools),
        turns=2,
        stop_reason="end_turn",
        usage=TokenUsage(**usage),
        model_served="claude-opus-5-5",
    )
    score = harness.score_answer(case, case.required_tools, run.final_text)
    return harness.CaseResult(case, run, score)


def test_summary_applies_the_bar_of_eleven_in_twelve(harness: ModuleType) -> None:
    case = case_from(harness)

    eleven = [_result(harness, case, n < 11) for n in range(12)]
    ten = [_result(harness, case, n < 10) for n in range(12)]

    assert harness.summarize(eleven)["meets_bar"] is True
    assert harness.summarize(ten)["meets_bar"] is False
    assert harness.summarize(eleven)["bar"] == "11/12"
    assert harness.summarize([])["meets_bar"] is False


def test_summary_reports_accuracy_tool_calls_tokens_and_cost(harness: ModuleType) -> None:
    case = case_from(harness, required_tools=["fetch_mibor", "fetch_fx_rate"])
    results = [
        _result(harness, case, True, input_tokens=1_000_000, output_tokens=0),
        _result(harness, case, False, input_tokens=0, output_tokens=1_000_000),
    ]

    summary = harness.summarize(results)

    assert summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["tool_selection_accuracy"] == 1.0
    assert summary["avg_tool_calls"] == 2.0
    assert (summary["input_tokens"], summary["output_tokens"]) == (1_000_000, 1_000_000)
    assert summary["estimated_cost_usd"] == pytest.approx(24.0)  # $4 in + $20 out


def test_cost_counts_cache_tokens_at_their_own_rates(harness: ModuleType) -> None:
    usage = TokenUsage(cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)

    assert harness.estimate_cost(usage) == pytest.approx(5.20)


def test_results_table_and_summary_text(harness: ModuleType) -> None:
    case = case_from(harness)
    good, bad = _result(harness, case, True, input_tokens=10), _result(harness, case, False)

    table = harness.render_results([good, bad])
    summary = harness.render_summary(harness.summarize([good, bad]))

    assert "PASS" in table
    assert "FAIL" in table
    assert "✓" in table
    assert "tools" in table.splitlines()[0]
    assert "answer check failed" in table
    assert "BELOW BAR" in summary
    assert "estimated cost" in summary


# --------------------------------------------------------------------------- dry run
def test_dry_run_verifies_expected_values_against_the_fixture_db(
    harness: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = harness.main(["--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "12 cases: valid" in out
    assert "usd_invoice_inr_and_settlement" in out


def test_dry_run_catches_a_wrong_expected_number(
    harness: ModuleType,
    raw_cases: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_cases["cases"][0]["expect"][0]["value"] = 120000  # the fixture DB says 115091.88
    broken = tmp_path / "cases.json"
    broken.write_text(json.dumps(raw_cases), encoding="utf-8")

    code = harness.main(["--dry-run", "--cases", str(broken)])

    out = capsys.readouterr().out
    assert code == 1
    assert "usd_invoice_inr_and_settlement" in out
    assert "120000" in out


def test_dry_run_catches_a_wrong_ground_truth_and_a_bad_golden_answer(
    harness: ModuleType,
    raw_cases: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_cases["cases"][1]["ground_truth"][0]["checks"][0]["value"] = True
    raw_cases["cases"][2]["golden_answer"] = "Settles on 9 April."
    broken = tmp_path / "cases.json"
    broken.write_text(json.dumps(raw_cases), encoding="utf-8")

    code = harness.main(["--dry-run", "--cases", str(broken)])

    out = capsys.readouterr().out
    assert code == 1
    assert "mumbai_holiday_mar31: ground truth #0" in out
    assert "t2_settlement_skipped_days: golden answer fails" in out


def test_invalid_case_file_exits_1_and_lists_problems(
    harness: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "cases.json"
    bad.write_text(json.dumps({"fixed_now": "x", "cases": []}), encoding="utf-8")

    code = harness.main(["--dry-run", "--cases", str(bad)])

    assert code == 1
    assert "invalid case file" in capsys.readouterr().err


# --------------------------------------------------------------------------- run path
def _factory(backend: FakeBackend) -> Any:
    return lambda db_path, fixed_now: backend


def test_main_exits_2_without_credentials(
    harness: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    nobody = SimpleNamespace(api_key=None, auth_token=None, credentials=None)

    code = harness.main(["--results-dir", str(tmp_path)], client_factory=lambda: nobody)

    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_main_rejects_a_filter_that_matches_nothing(
    harness: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code = harness.main(["--filter", "zzz-nothing"], client_factory=lambda: ScriptedClient())

    assert code == 1
    assert "no case matches" in capsys.readouterr().err


def test_main_runs_a_case_scores_it_and_writes_results(
    harness: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    golden = next(
        c for c in harness.load_case_file(CASES_FILE).cases if c.id == "mumbai_holiday_mar31"
    )
    client = ScriptedClient(
        message(
            [tool_use("check_business_day", {"date": "2026-03-31", "office": "mumbai"})], "tool_use"
        ),
        message([text(golden.golden_answer)]),
    )
    out_copy = tmp_path / "copy.json"

    code = harness.main(
        [
            "--filter",
            "mumbai_holiday_mar31",
            "--results-dir",
            str(tmp_path / "res"),
            "--json",
            str(out_copy),
        ],
        client_factory=lambda: client,
        backend_factory=_factory(FakeBackend()),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out
    assert "MEETS BAR" in out
    written = list((tmp_path / "res").glob("*.json"))
    assert len(written) == 1
    document = json.loads(written[0].read_text(encoding="utf-8"))
    assert json.loads(out_copy.read_text(encoding="utf-8")) == document
    assert document["model"] == "claude-opus-5-5"
    assert document["fallback"] is True
    assert document["summary"]["passed"] == 1
    case = document["cases"][0]
    assert case["id"] == "mumbai_holiday_mar31"
    assert case["passed"] is True
    assert case["tool_calls"][0]["name"] == "check_business_day"
    assert case["usage"]["input_tokens"] == 20
    assert "2026-09-30T15:00:00+05:30" in client.calls[0]["system"][0]["text"]


def test_main_exits_1_below_the_bar_and_names_the_failure(
    harness: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = ScriptedClient(message([text("I think it is open.")]))  # no tool call, no facts

    code = harness.main(
        ["--filter", "mumbai_holiday_mar31", "--no-fallback", "--results-dir", str(tmp_path)],
        client_factory=lambda: client,
        backend_factory=_factory(FakeBackend()),
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "required tool not called: check_business_day" in out
    assert "BELOW BAR" in out
    assert client.calls[0]["route"] == "plain"


def test_an_api_error_fails_the_case_instead_of_crashing_the_run(
    harness: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)
    client = ScriptedClient(error)

    code = harness.main(
        ["--filter", "gst_rate", "--results-dir", str(tmp_path)],
        client_factory=lambda: client,
        backend_factory=_factory(FakeBackend()),
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "could not reach the API" in out
