"""``GET /v1/holidays``."""

from __future__ import annotations

from fastapi.testclient import TestClient

from imda.models import Dataset, Source, SourceStatus
from imda.store.repo import Store
from tests.api.helpers import assert_envelope, assert_error, body


def test_office_and_year_returns_every_holiday_with_provenance(client: TestClient) -> None:
    parsed = body(client.get("/v1/holidays?office=mumbai&year=2026"))

    assert_envelope(parsed)
    assert parsed["meta"]["count"] == 22
    republic = next(h for h in parsed["data"] if h["date"] == "2026-01-26")
    assert republic == {
        "office": "mumbai",
        "date": "2026-01-26",
        "weekday": "Monday",
        "name": "Republic Day",
        "kind": "ni_act",
    }
    assert {h["kind"] for h in parsed["data"]} == {"ni_act", "closing_of_accounts"}
    assert parsed["provenance"][0]["dataset"] == "holidays"


def test_month_filter(client: TestClient) -> None:
    parsed = body(client.get("/v1/holidays?office=mumbai&year=2026&month=4"))

    assert [h["date"] for h in parsed["data"]] == ["2026-04-01", "2026-04-03", "2026-04-14"]


def test_office_is_case_insensitive(client: TestClient) -> None:
    assert body(client.get("/v1/holidays?office=Mumbai&year=2026"))["meta"]["count"] == 22


def test_without_office_returns_all_loaded_offices_and_warns_about_gaps(
    client: TestClient,
) -> None:
    parsed = body(client.get("/v1/holidays?year=2026"))

    assert {h["office"] for h in parsed["data"]} == {"mumbai"}
    assert "not loaded for 33 of 34 offices" in parsed["meta"]["warnings"][0]


def test_year_not_loaded_for_office_is_a_409_with_a_hint(client: TestClient) -> None:
    parsed = assert_error(
        client.get("/v1/holidays?office=mumbai&year=2027"), 409, "CALENDAR_DATA_MISSING"
    )

    assert parsed["error"]["details"] == {
        "office": "mumbai",
        "year": 2027,
        "hint": "imda backfill --datasets holidays --from 2027-01-01",
    }


def test_unknown_office_is_404(client: TestClient) -> None:
    parsed = assert_error(
        client.get("/v1/holidays?office=atlantis&year=2026"), 404, "OFFICE_NOT_FOUND"
    )

    assert parsed["error"]["details"] == {"office": "atlantis"}


def test_year_is_required_and_month_is_bounded(client: TestClient) -> None:
    assert_error(client.get("/v1/holidays?office=mumbai"), 422, "INVALID_REQUEST")
    assert_error(
        client.get("/v1/holidays?office=mumbai&year=2026&month=13"), 422, "INVALID_REQUEST"
    )
    assert_error(client.get("/v1/holidays?office=mumbai&year=abc"), 422, "INVALID_REQUEST")


def test_degraded_source_sets_meta_flag_and_warning(client: TestClient, store: Store) -> None:
    store.set_source_health(
        Source.RBI, Dataset.HOLIDAYS, SourceStatus.DEGRADED, error="layout drift"
    )

    parsed = body(client.get("/v1/holidays?office=mumbai&year=2026"))

    assert parsed["meta"]["degraded"] is True
    assert any("rbi/holidays is degraded" in w for w in parsed["meta"]["warnings"])
    assert parsed["meta"]["count"] == 22  # last good data is still served


def test_month_with_no_holidays_still_has_provenance(client: TestClient) -> None:
    parsed = body(client.get("/v1/holidays?office=mumbai&year=2026&month=7"))

    assert parsed["data"] == []
    (entry,) = parsed["provenance"]
    assert (entry["source"], entry["dataset"]) == ("rbi", "holidays")
    assert entry["source_url"].startswith("https://www.rbi.org.in/")
    assert entry["fetched_at"] is not None
