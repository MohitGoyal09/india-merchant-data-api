"""``GET /v1/settlement/eta``."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from imda.config import Settings
from tests.api.conftest import MakeClient
from tests.api.helpers import assert_envelope, assert_error, body

URL = "/v1/settlement/eta"
FRIDAY = "2026-03-27T11:00:00%2B05:30"


def test_friday_capture_skips_weekend_and_holidays(client: TestClient) -> None:
    parsed = body(client.get(f"{URL}?captured_at={FRIDAY}&office=mumbai"))

    assert_envelope(parsed)
    data = parsed["data"]
    assert data["eta_date"] == "2026-04-02"
    assert data["capture_date"] == "2026-03-27"
    assert data["cycle_days"] == 2
    assert data["mode"] == "working_days"
    assert data["counted_days"] == ["2026-03-30", "2026-04-02"]
    assert [s["date"] for s in data["skipped"]] == [
        "2026-03-28",
        "2026-03-29",
        "2026-03-31",
        "2026-04-01",
    ]
    assert data["skipped"][0] == {"date": "2026-03-28", "reason": "4th Saturday"}
    assert data["disclaimer"].startswith("Indicative estimate based on RBI holidays")
    assert "not Razorpay's settlement engine" in data["disclaimer"]
    assert parsed["provenance"][0]["dataset"] == "holidays"


def test_unescaped_plus_in_the_offset_is_tolerated(client: TestClient) -> None:
    parsed = body(client.get(f"{URL}?captured_at=2026-03-27T11:00:00+05:30&office=mumbai"))

    assert parsed["data"]["eta_date"] == "2026-04-02"
    assert parsed["data"]["captured_at"] == "2026-03-27T11:00:00+05:30"


def test_z_suffix_and_ist_date_conversion(client: TestClient) -> None:
    # 20:00 UTC on 27 Mar is 01:30 IST on Saturday 28 Mar.
    parsed = body(client.get(f"{URL}?captured_at=2026-03-27T20:00:00Z&office=mumbai"))

    assert parsed["data"]["capture_date"] == "2026-03-28"


def test_calendar_then_roll_mode_and_cycle_days(client: TestClient) -> None:
    parsed = body(
        client.get(f"{URL}?captured_at={FRIDAY}&office=mumbai&mode=calendar_then_roll&cycle_days=2")
    )

    assert parsed["data"]["eta_date"] == "2026-03-30"
    assert parsed["data"]["counted_days"] == []
    assert parsed["data"]["mode"] == "calendar_then_roll"


def test_cycle_days_default_comes_from_settings(
    settings: Settings, make_client: MakeClient
) -> None:
    custom = settings.model_copy(update={"settlement_cycle_days": 1})

    parsed = body(make_client(custom=custom).get(f"{URL}?captured_at={FRIDAY}&office=mumbai"))

    assert parsed["data"]["cycle_days"] == 1
    assert parsed["data"]["eta_date"] == "2026-03-30"


def test_naive_datetime_is_rejected(client: TestClient) -> None:
    parsed = assert_error(
        client.get(f"{URL}?captured_at=2026-03-27T11:00:00&office=mumbai"), 422, "INVALID_REQUEST"
    )

    assert "UTC offset" in parsed["error"]["details"]["errors"][0]["message"]


@pytest.mark.parametrize(
    "query",
    [
        "captured_at=garbage&office=mumbai",
        f"captured_at={FRIDAY}",
        "office=mumbai",
        f"captured_at={FRIDAY}&office=mumbai&cycle_days=31",
        f"captured_at={FRIDAY}&office=mumbai&cycle_days=-1",
        f"captured_at={FRIDAY}&office=mumbai&mode=fast",
    ],
)
def test_invalid_requests(client: TestClient, query: str) -> None:
    assert_error(client.get(f"{URL}?{query}"), 422, "INVALID_REQUEST")


def test_unknown_office_and_missing_calendar_year(client: TestClient) -> None:
    assert_error(client.get(f"{URL}?captured_at={FRIDAY}&office=oz"), 404, "OFFICE_NOT_FOUND")
    parsed = assert_error(
        client.get(f"{URL}?captured_at=2027-03-26T11:00:00%2B05:30&office=mumbai"),
        409,
        "CALENDAR_DATA_MISSING",
    )
    assert parsed["error"]["details"]["year"] == 2027
