"""The drift canary: one small live-shaped sample per dataset, health only, no data writes."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from imda.health.canary import MAX_REQUESTS, CanaryReport, HolidayYears, run_canary
from imda.ingest.common import ExchangeLog
from imda.models import Dataset, Source, SourceStatus
from imda.store.repo import Store
from tests.unit.test_ingest import (
    FBIL_FX_URL,
    FBIL_MIBOR_URL,
    HOLIDAYS_URL,
    RBI_FX_URL,
    TODAY,
    FakeUpstream,
    count,
    event_names,
    health,
    read,
    run_backfill,
    run_refresh,
)

DATA_TABLES = ("fx_rates", "mibor_rates", "holidays", "offices", "holiday_years")
KEYS = {
    ("rbi", "holidays"),
    ("rbi", "fx_reference_rates"),
    ("fbil", "fx_reference_rates"),
    ("fbil", "mibor_overnight"),
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "canary.sqlite3") as opened:
        yield opened


def canary(store: Store, client: FakeUpstream, **kwargs: object) -> CanaryReport:
    log = ExchangeLog(store)
    client.attach(log)
    return run_canary(store, client, today=TODAY, exchange_log=log, **kwargs)  # type: ignore[arg-type]


def result(report: CanaryReport, source: Source, dataset: Dataset):
    return next(r for r in report.results if (r.source, r.dataset) == (source, dataset))


def fbil_fx_rows() -> list[dict[str, object]]:
    return json.loads(read("fbil/fx_2026_09.json"))


def source_events(store: Store) -> list[str]:
    return [e for e in event_names(store) if e.startswith("source.")]


def test_healthy_upstream_marks_every_dataset_ok(store: Store) -> None:
    client = FakeUpstream()

    report = canary(store, client)

    assert report.status is SourceStatus.OK
    assert {(r.source.value, r.dataset.value) for r in report.results} == KEYS
    assert all(r.status is SourceStatus.OK for r in report.results)
    assert all(r.drift is not None and not r.drift.drifted for r in report.results)
    assert set(health(store).items()) == {(k, "ok") for k in KEYS}
    assert source_events(store) == []


def test_sample_is_small(store: Store) -> None:
    client = FakeUpstream()

    report = canary(store, client)

    assert len(client.requests) <= MAX_REQUESTS
    assert report.requests == len(client.requests)
    assert MAX_REQUESTS == 6


def test_samples_ask_for_the_current_month_and_the_last_seven_days(store: Store) -> None:
    client = FakeUpstream()

    canary(store, client)

    posts = [r for r in client.requests if r.method == "POST" and r.url == HOLIDAYS_URL]
    assert [(r.form or {})["drYear"] + "-" + (r.form or {})["drMonth"] for r in posts] == [
        "2026-10"
    ]
    assert (posts[0].form or {})["drRegionalOffice"] == "0"
    rbi_post = next(r for r in client.requests if r.url == RBI_FX_URL and r.method == "POST")
    assert (rbi_post.form or {})["txtFromDate"] == "26/09/2026"
    assert (rbi_post.form or {})["txtToDate"] == "02/10/2026"
    for url in (FBIL_FX_URL, FBIL_MIBOR_URL):
        sent = next(r for r in client.requests if r.url == url)
        assert dict(sent.params)["fromDate"] == "2026-09-26"
        assert dict(sent.params)["toDate"] == "2026-10-02"


def test_canary_writes_no_data_only_health_run_log_and_events(store: Store) -> None:
    client = FakeUpstream()

    report = canary(store, client)

    assert all(count(store, table) == 0 for table in DATA_TABLES)
    assert count(store, "fetch_log") == len(client.requests)
    [run] = store.runs(5)
    assert run["kind"] == "canary"
    assert run["status"] == "ok"
    assert run["run_id"] == report.run_id
    assert run["summary"]["requests"] == len(client.requests)  # type: ignore[index]


def test_payloads_are_logged_even_when_the_client_is_not_wired(store: Store) -> None:
    run_canary(store, FakeUpstream(), today=TODAY)

    # every payload the adapters received is logged; only the hidden form-state GET is not
    assert count(store, "fetch_log") == 5


def test_health_stores_the_fingerprint_for_later_comparison(store: Store) -> None:
    canary(store, FakeUpstream())

    rows = {(r["source"], r["dataset"]): r for r in store.source_health()}
    fingerprint = rows[("fbil", "mibor_overnight")]["fingerprint"]
    assert isinstance(fingerprint, dict)
    assert "key_sets" in fingerprint


# ---------------------------------------------------------------- drift and breakage
def test_a_renamed_json_key_is_drift_and_degrades_the_source(store: Store) -> None:
    rows = [
        {("remarks" if k == "comments" else k): v for k, v in row.items()} for row in fbil_fx_rows()
    ]
    client = FakeUpstream(garbage={FBIL_FX_URL: json.dumps(rows).encode()})

    report = canary(store, client)

    fx = result(report, Source.FBIL, Dataset.FX)
    assert fx.status is SourceStatus.DEGRADED
    assert fx.drift is not None
    assert fx.drift.drifted
    assert "remarks" in str(fx.error)
    assert report.status is SourceStatus.DEGRADED
    assert health(store)[("fbil", "fx_reference_rates")] == "degraded"
    assert health(store)[("fbil", "mibor_overnight")] == "ok"
    assert source_events(store) == ["source.degraded"]


def test_a_truncated_page_is_broken(store: Store) -> None:
    page = read("rbi/fx_2026_09.html")
    client = FakeUpstream(garbage={RBI_FX_URL: page[: len(page) // 2]})

    report = canary(store, client)

    fx = result(report, Source.RBI, Dataset.FX)
    assert fx.status is SourceStatus.BROKEN
    assert "truncated" in str(fx.error)
    assert report.status is SourceStatus.BROKEN
    assert health(store)[("rbi", "fx_reference_rates")] == "broken"
    assert health(store)[("rbi", "holidays")] == "ok"


def test_an_upstream_error_degrades_that_source_only(store: Store) -> None:
    client = FakeUpstream(down=("fbil.org.in",))

    report = canary(store, client)

    assert result(report, Source.FBIL, Dataset.FX).status is SourceStatus.DEGRADED
    assert result(report, Source.FBIL, Dataset.MIBOR).status is SourceStatus.DEGRADED
    assert "connection refused" in str(result(report, Source.FBIL, Dataset.FX).error)
    assert result(report, Source.RBI, Dataset.FX).status is SourceStatus.OK
    assert report.status is SourceStatus.DEGRADED
    assert source_events(store) == ["source.degraded", "source.degraded"]
    assert len(client.requests) <= MAX_REQUESTS


def test_an_empty_week_parses_so_it_is_not_broken(store: Store) -> None:
    """FBIL can lag by days (it did on 2026-10-02); lateness is the freshness check's job."""
    client = FakeUpstream(garbage={FBIL_MIBOR_URL: b"[]"})

    report = canary(store, client)

    mibor = result(report, Source.FBIL, Dataset.MIBOR)
    assert mibor.status is SourceStatus.OK
    assert mibor.drift is not None
    assert not mibor.drift.drifted
    assert "empty" in str(mibor.drift.note)


def test_an_empty_holiday_month_is_fine(store: Store) -> None:
    client = FakeUpstream(garbage={HOLIDAYS_URL: read("rbi/holidays_all_2005_06_empty.html")})

    report = canary(store, client)

    # the GET page is replaced too, so only the layout matters: it must not be called drift
    holidays = result(report, Source.RBI, Dataset.HOLIDAYS)
    assert holidays.drift is None or not holidays.drift.drifted


def test_recovery_after_a_bad_canary_emits_recovered_once(store: Store) -> None:
    canary(store, FakeUpstream(down=("fbil.org.in",)))
    canary(store, FakeUpstream())
    canary(store, FakeUpstream())

    assert source_events(store) == [
        "source.degraded",
        "source.degraded",
        "source.recovered",
        "source.recovered",
    ]
    assert set(health(store).values()) == {"ok"}
    row = next(r for r in store.source_health() if r["dataset"] == "mibor_overnight")
    assert row["drift"] == {"drifted": False}


def test_repeated_drift_does_not_repeat_the_event(store: Store) -> None:
    rows = [{**row, "extra": 1} for row in fbil_fx_rows()]
    bad = json.dumps(rows).encode()

    canary(store, FakeUpstream(garbage={FBIL_FX_URL: bad}))
    canary(store, FakeUpstream(garbage={FBIL_FX_URL: bad}))

    assert source_events(store) == ["source.degraded"]


def test_missing_baselines_are_a_first_run_not_drift(store: Store) -> None:
    report = canary(store, FakeUpstream(), baselines={})

    assert report.status is SourceStatus.OK
    assert all("baseline" in str(r.drift.note) for r in report.results if r.drift)


def test_report_serialises(store: Store) -> None:
    rows = [{**row, "extra": 1} for row in fbil_fx_rows()]
    report = canary(store, FakeUpstream(garbage={FBIL_FX_URL: json.dumps(rows).encode()}))

    data = report.as_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["status"] == "degraded"
    assert data["requests"] == report.requests
    results = data["results"]
    assert isinstance(results, dict)
    assert results["fbil/fx_reference_rates"]["drift"]["drifted"] is True  # type: ignore[index]
    assert results["rbi/holidays"]["status"] == "ok"  # type: ignore[index]


def test_today_is_taken_from_the_argument(store: Store) -> None:
    client = FakeUpstream()
    log = ExchangeLog(store)
    client.attach(log)

    run_canary(store, client, today=dt.date(2026, 9, 15), exchange_log=log)

    posts = [r for r in client.requests if r.method == "POST" and r.url == HOLIDAYS_URL]
    assert (posts[0].form or {})["drMonth"] == "9"


# ---------------------------------------------------------------- next year's holidays
def load_2026(store: Store) -> None:
    """Load the whole of 2026 (a prior backfill) so the canary has a latest loaded year."""
    window = (dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    run_backfill(store, FakeUpstream(), {Dataset.HOLIDAYS}, window=window)


def year_events(store: Store) -> list[dict[str, object]]:
    return [
        e["payload"]  # type: ignore[misc]
        for e in store.events_since(None, 1000)
        if e["event"] == "holidays.year_available"
    ]


def test_canary_reports_the_years_rbi_offers_and_the_latest_loaded(store: Store) -> None:
    load_2026(store)

    report = canary(store, FakeUpstream())

    years = report.holiday_years
    assert years is not None
    assert years.latest_year_offered == 2026
    assert years.latest_year_loaded == 2026
    assert years.new_year_available is False
    assert years.years_offered[0] == 2001
    assert years.years_offered[-1] == 2026
    assert year_events(store) == []


def test_a_new_year_keeps_status_ok_and_records_one_event(store: Store) -> None:
    load_2026(store)

    report = canary(store, FakeUpstream(extra_years=(2027,)))

    assert report.status is SourceStatus.OK
    years = report.holiday_years
    assert years is not None
    assert years.years_offered[-2:] == (2026, 2027)
    assert years.latest_year_offered == 2027
    assert years.latest_year_loaded == 2026
    assert years.new_year_available is True
    [event] = year_events(store)
    assert event["year"] == 2027
    assert event["latest_year_loaded"] == 2026
    assert source_events(store) == []  # not a health transition


def test_the_event_is_recorded_once_per_year(store: Store) -> None:
    load_2026(store)

    canary(store, FakeUpstream(extra_years=(2027,)))
    canary(store, FakeUpstream(extra_years=(2027,)))
    third = canary(store, FakeUpstream(extra_years=(2027,)))

    assert third.holiday_years is not None
    assert third.holiday_years.new_year_available is True  # still reported every run
    assert [e["year"] for e in year_events(store)] == [2027]

    canary(store, FakeUpstream(extra_years=(2027, 2028)))
    assert [e["year"] for e in year_events(store)] == [2027, 2028]


def test_loading_the_new_year_clears_the_flag(store: Store) -> None:
    load_2026(store)
    canary(store, FakeUpstream(extra_years=(2027,)))
    run_refresh(store, FakeUpstream(extra_years=(2027,)))

    report = canary(store, FakeUpstream(extra_years=(2027,)))

    years = report.holiday_years
    assert years is not None
    assert years.latest_year_loaded == 2027
    assert years.new_year_available is False
    assert [e["year"] for e in year_events(store)] == [2027]


def test_an_empty_database_is_not_a_new_year(store: Store) -> None:
    report = canary(store, FakeUpstream(extra_years=(2027,)))

    years = report.holiday_years
    assert years is not None
    assert years.latest_year_loaded is None
    assert years.new_year_available is False
    assert year_events(store) == []


def test_year_facts_reach_the_report_dict_and_the_stored_health_row(store: Store) -> None:
    load_2026(store)

    report = canary(store, FakeUpstream(extra_years=(2027,)))

    expected = {
        "years_offered": list(range(2001, 2028)),
        "latest_year_offered": 2027,
        "latest_year_loaded": 2026,
        "new_year_available": True,
    }
    data = report.as_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["holiday_years"] == expected
    row = next(r for r in store.source_health() if r["dataset"] == "holidays")
    assert row["drift"]["holiday_years"] == expected  # type: ignore[index]
    assert row["drift"]["drifted"] is False  # type: ignore[index]


def test_a_missing_year_dropdown_is_broken(store: Store) -> None:
    page = read("rbi/holidays_page.html").replace(b'name="drYear"', b'name="drYearX"')

    report = canary(store, FakeUpstream(garbage={HOLIDAYS_URL: page}))

    holidays = result(report, Source.RBI, Dataset.HOLIDAYS)
    assert holidays.status is SourceStatus.BROKEN
    assert "drYear" in str(holidays.error)
    assert report.holiday_years is None


def test_holiday_years_round_trip_and_reject_malformed_data() -> None:
    years = HolidayYears.compare((2025, 2026, 2027), frozenset({2025, 2026}))

    assert HolidayYears.from_dict(years.as_dict()) == years
    assert HolidayYears.from_dict("nope") is None
    assert HolidayYears.from_dict({"years_offered": "x"}) is None
