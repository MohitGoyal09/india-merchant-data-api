"""Fingerprint comparison and the packaged baselines."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from imda.health.drift import (
    DriftReport,
    baseline_key,
    check_drift,
    compare_fingerprint,
    load_baselines,
)
from imda.ingest.common import ExchangeLog
from imda.ingest.refresh import refresh
from imda.models import Currency, Dataset, Source, SourceStatus
from imda.store.repo import Store
from tests.unit.test_ingest import (
    FBIL_FX_URL,
    TODAY,
    FakeUpstream,
    event_names,
    health,
    read,
    run_backfill,
)

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "drift.sqlite3") as opened:
        yield opened


def test_identical_fingerprints_do_not_drift() -> None:
    fingerprint = {"layout": "month_matrix", "form_fields": ["a", "b"]}
    report = compare_fingerprint(dict(fingerprint), fingerprint)
    assert report == DriftReport(drifted=False)


def test_added_and_removed_keys_are_reported() -> None:
    report = compare_fingerprint({"a": 1, "b": 2}, {"a": 1, "c": 3})
    assert report.drifted
    assert report.added_keys == ("c",)
    assert report.removed_keys == ("b",)
    assert report.changed == {}


def test_changed_values_carry_old_and_new() -> None:
    report = compare_fingerprint({"layout": "month_matrix"}, {"layout": "office_list"})
    assert report.drifted
    assert report.changed == {"layout": ("month_matrix", "office_list")}


def test_nested_dicts_are_compared_by_dotted_path() -> None:
    baseline = {"select_option_counts": {"drMonth": 13, "drRegionalOffice": 35}}
    current = {"select_option_counts": {"drMonth": 13, "drRegionalOffice": 36, "drNew": 2}}
    report = compare_fingerprint(baseline, current)
    assert report.changed == {"select_option_counts.drRegionalOffice": (35, 36)}
    assert report.added_keys == ("select_option_counts.drNew",)


def test_lists_are_compared_structurally_and_in_order() -> None:
    baseline = {"form_fields": ["a", "b"], "key_sets": [["x", "y"]]}
    assert not compare_fingerprint(baseline, {"form_fields": ["a", "b"], "key_sets": [["x", "y"]]})
    report = compare_fingerprint(baseline, {"form_fields": ["a", "c"], "key_sets": [["x", "y"]]})
    assert report.changed == {"form_fields": (["a", "b"], ["a", "c"])}


def test_tuples_equal_lists_after_json_round_trip() -> None:
    assert not compare_fingerprint({"k": ["a", "b"]}, {"k": ("a", "b")}).drifted


def test_type_change_is_a_change() -> None:
    report = compare_fingerprint({"n": 1}, {"n": "1"})
    assert report.changed == {"n": (1, "1")}


def test_dict_replaced_by_scalar_is_drift() -> None:
    report = compare_fingerprint({"a": {"b": 1}}, {"a": 5})
    assert report.drifted
    assert report.removed_keys == ("a.b",)
    assert report.added_keys == ("a",)


def test_ignored_keys_are_skipped_even_when_nested() -> None:
    baseline = {"_ignore": ["row_count", "value_types.comments"], "row_count": 64, "k": 1}
    baseline["value_types"] = {"comments": ["str"], "rate": ["float"]}
    current = {
        "row_count": 999,
        "k": 1,
        "value_types": {"comments": ["NoneType", "str"], "rate": ["float"]},
    }
    assert not compare_fingerprint(baseline, current).drifted


def test_ignore_does_not_hide_other_keys() -> None:
    report = compare_fingerprint({"_ignore": ["row_count"], "k": 1}, {"row_count": 5, "k": 2})
    assert report.changed == {"k": (1, 2)}


def test_ignore_matches_whole_path_segments_only() -> None:
    report = compare_fingerprint({"_ignore": ["row"], "row_count": 1}, {"row_count": 2})
    assert report.changed == {"row_count": (1, 2)}


def test_missing_baseline_is_not_drift_and_says_so() -> None:
    report = compare_fingerprint(None, {"a": 1})
    assert not report.drifted
    assert "baseline" in str(report.note)


def test_report_serialises_for_source_health() -> None:
    report = compare_fingerprint({"a": 1, "b": 2, "n": 1}, {"a": 1, "c": 3, "n": 2})
    data = report.as_dict()
    assert data == {
        "drifted": True,
        "added_keys": ["c"],
        "removed_keys": ["b"],
        "changed": {"n": {"old": 1, "new": 2}},
    }
    assert json.loads(json.dumps(data)) == data
    assert "added c" in report.summary()
    assert "removed b" in report.summary()
    assert "n: 1 -> 2" in report.summary()


def test_summary_of_a_clean_report() -> None:
    assert DriftReport(drifted=False).summary() == "no drift"


# ---------------------------------------------------------------- baselines
def test_packaged_baselines_cover_every_fingerprinted_dataset() -> None:
    assert set(load_baselines()) == {
        "rbi/holidays#month_matrix",
        "rbi/holidays#no_holidays",
        "rbi/holidays#office_list",
        "rbi/fx_reference_rates#rate_table",
        "rbi/fx_reference_rates#no_data",
        "fbil/fx_reference_rates",
        "fbil/mibor_overnight",
    }


def test_load_baselines_reads_an_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text('{"x/y": {"k": 1}}')
    assert load_baselines(path) == {"x/y": {"k": 1}}


def test_baseline_key_adds_the_layout_when_the_fingerprint_has_one() -> None:
    assert (
        baseline_key(Source.RBI, Dataset.HOLIDAYS, {"layout": "office_list"})
        == "rbi/holidays#office_list"
    )
    assert baseline_key(Source.FBIL, Dataset.MIBOR, {"row_count": 3}) == "fbil/mibor_overnight"


def test_check_drift_looks_up_the_baseline_by_key() -> None:
    baselines = {"fbil/mibor_overnight": {"k": 1}}
    assert check_drift(baselines, Source.FBIL, Dataset.MIBOR, {"k": 1}).drifted is False
    assert check_drift(baselines, Source.FBIL, Dataset.MIBOR, {"k": 2}).drifted is True
    unknown = check_drift(baselines, Source.FBIL, Dataset.FX, {"k": 1})
    assert unknown.drifted is False
    assert unknown.note is not None


def _update_script() -> ModuleType:
    path = ROOT / "scripts" / "update_baselines.py"
    spec = importlib.util.spec_from_file_location("update_baselines", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_baselines_match_the_recorded_fixtures() -> None:
    """Guards against stale baselines: regenerate in memory and compare."""
    rebuilt = _update_script().build_baselines()
    assert rebuilt == load_baselines(), "run `uv run python scripts/update_baselines.py`"


def test_every_recorded_fixture_matches_its_baseline() -> None:
    baselines = load_baselines()
    for key, fingerprint in _update_script().fixture_fingerprints():
        report = compare_fingerprint(baselines[key], fingerprint)
        assert not report.drifted, (key, report.summary())


def test_fixture_fingerprint_helper_is_offline_and_deterministic() -> None:
    module = _update_script()
    first = list(module.fixture_fingerprints())
    assert first == list(module.fixture_fingerprints())
    assert {k for k, _ in first} == set(load_baselines())


def test_an_empty_sample_has_no_shape_to_compare() -> None:
    baselines = {"fbil/fx_reference_rates": {"key_sets": [["a"]]}}
    empty = {"row_count": 0, "key_sets": [], "value_types": {}}
    report = check_drift(baselines, Source.FBIL, Dataset.FX, empty)
    assert not report.drifted
    assert "empty" in str(report.note)


# ---------------------------------------------------------------- drift during ingest
def _drifted_fbil_fx() -> bytes:
    """The recorded September FX answer, with one new field on every row (still parses)."""
    rows = json.loads(read("fbil/fx_2026_09.json"))
    return json.dumps([{**row, "newField": 1} for row in rows]).encode()


def _fbil_health(store: Store) -> dict[str, object]:
    return next(
        r
        for r in store.source_health()
        if r["dataset"] == "fx_reference_rates" and r["source"] == "fbil"
    )


def source_events(store: Store) -> list[str]:
    return [name for name in event_names(store) if name.startswith("source.")]


def _fx_backfill(store: Store, client: FakeUpstream, **kwargs: object) -> None:
    run_backfill(store, client, {Dataset.FX}, **kwargs)


def test_clean_ingest_is_ok_and_records_a_no_drift_report(store: Store) -> None:
    _fx_backfill(store, FakeUpstream())

    row = _fbil_health(store)
    assert row["status"] == "ok"
    assert row["drift"] == {"drifted": False}
    assert source_events(store) == []


def test_drifted_shape_degrades_the_source_but_keeps_the_new_data(store: Store) -> None:
    client = FakeUpstream(garbage={FBIL_FX_URL: _drifted_fbil_fx()})

    run_backfill(store, client, {Dataset.FX})

    row = _fbil_health(store)
    assert row["status"] == "degraded"
    drift = row["drift"]
    assert isinstance(drift, dict)
    assert drift["drifted"] is True
    assert "value_types.newField" in drift["added_keys"]
    assert "newField" in str(row["last_error"])
    assert store.fx_rates(Currency.USD, dt.date(2026, 9, 1), dt.date(2026, 9, 30), Source.FBIL)
    assert health(store)[("rbi", "fx_reference_rates")] == "ok"


def test_drift_task_is_reported_in_the_run_summary(store: Store) -> None:
    client = FakeUpstream(garbage={FBIL_FX_URL: _drifted_fbil_fx()})

    summary = run_backfill(store, client, {Dataset.FX})

    task = next(t for t in summary.tasks if t.key == "fbil/fx_reference_rates")
    assert task.status == "ok"
    assert task.health is SourceStatus.DEGRADED
    assert task.drift is not None
    assert task.drift.drifted
    assert "drift" in summary.as_dict()["tasks"]["fbil/fx_reference_rates"]  # type: ignore[index]
    assert "drift" not in summary.as_dict()["tasks"]["rbi/fx_reference_rates"]  # type: ignore[index]


def test_drift_emits_degraded_once_and_recovery_clears_it(store: Store) -> None:
    drifted = FakeUpstream(garbage={FBIL_FX_URL: _drifted_fbil_fx()})

    _fx_backfill(store, FakeUpstream())
    _fx_backfill(store, drifted)
    _fx_backfill(store, drifted)
    assert source_events(store) == ["source.degraded"]
    degraded = next(e for e in store.events_since(None, 10) if e["event"] == "source.degraded")[
        "payload"
    ]
    assert degraded["previous"] == "ok"  # type: ignore[index]
    assert degraded["status"] == "degraded"  # type: ignore[index]
    assert "newField" in degraded["error"]  # type: ignore[index]

    _fx_backfill(store, FakeUpstream())
    assert source_events(store) == ["source.degraded", "source.recovered"]
    assert _fbil_health(store)["status"] == "ok"
    assert _fbil_health(store)["drift"] == {"drifted": False}


def test_first_drifted_run_still_emits_degraded(store: Store) -> None:
    _fx_backfill(store, FakeUpstream(garbage={FBIL_FX_URL: _drifted_fbil_fx()}))
    assert source_events(store) == ["source.degraded"]


def test_missing_baseline_is_ok_with_a_note(store: Store) -> None:
    _fx_backfill(store, FakeUpstream(), baselines={})

    row = _fbil_health(store)
    assert row["status"] == "ok"
    assert "baseline" in str(row["drift"])


def test_refresh_also_compares_against_baselines(store: Store) -> None:
    client = FakeUpstream(garbage={FBIL_FX_URL: _drifted_fbil_fx()})
    log = ExchangeLog(store)
    client.attach(log)

    refresh(store, client, today=TODAY, exchange_log=log)

    assert health(store)[("fbil", "fx_reference_rates")] == "degraded"
    assert health(store)[("fbil", "mibor_overnight")] == "ok"


def test_an_empty_holiday_month_is_not_drift() -> None:
    fingerprints = dict(_update_script().fixture_fingerprints())
    assert "rbi/holidays#no_holidays" in fingerprints
    report = check_drift(
        load_baselines(), Source.RBI, Dataset.HOLIDAYS, fingerprints["rbi/holidays#no_holidays"]
    )
    assert not report.drifted
