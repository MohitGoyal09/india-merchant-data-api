"""Business-day, next-business-days and the ICS feed."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from imda.api.ics import weekly_off_days
from tests.api.helpers import assert_envelope, assert_error, body


def test_business_day_reports_the_holiday_name(client: TestClient) -> None:
    parsed = body(client.get("/v1/calendar/business-day?date=2026-01-26&office=mumbai"))

    assert_envelope(parsed)
    assert parsed["data"] == {
        "date": "2026-01-26",
        "office": "mumbai",
        "weekday": "Monday",
        "is_business_day": False,
        "reason": "Republic Day",
    }
    assert parsed["provenance"][0]["dataset"] == "holidays"


@pytest.mark.parametrize(
    ("day", "reason"),
    [("2026-03-28", "4th Saturday"), ("2026-03-29", "Sunday"), ("2026-03-14", "2nd Saturday")],
)
def test_business_day_weekend_reasons(client: TestClient, day: str, reason: str) -> None:
    parsed = body(client.get(f"/v1/calendar/business-day?date={day}&office=mumbai"))

    assert parsed["data"]["is_business_day"] is False
    assert parsed["data"]["reason"] == reason


def test_business_day_true_has_null_reason(client: TestClient) -> None:
    parsed = body(client.get("/v1/calendar/business-day?date=2026-03-27&office=mumbai"))

    assert parsed["data"]["is_business_day"] is True
    assert parsed["data"]["reason"] is None


def test_business_day_errors(client: TestClient) -> None:
    assert_error(
        client.get("/v1/calendar/business-day?date=2026-03-27&office=nowhere"),
        404,
        "OFFICE_NOT_FOUND",
    )
    assert_error(
        client.get("/v1/calendar/business-day?date=2027-03-27&office=mumbai"),
        409,
        "CALENDAR_DATA_MISSING",
    )
    parsed = assert_error(
        client.get("/v1/calendar/business-day?date=27-03-2026&office=mumbai"),
        422,
        "INVALID_REQUEST",
    )
    assert parsed["error"]["details"]["errors"][0]["field"] == "date"
    assert_error(client.get("/v1/calendar/business-day?office=mumbai"), 422, "INVALID_REQUEST")
    assert_error(
        client.get("/v1/calendar/business-day?date=2026-02-30&office=mumbai"),
        422,
        "INVALID_REQUEST",
    )


def test_next_business_days_skips_weekend_and_holidays(client: TestClient) -> None:
    parsed = body(client.get("/v1/calendar/next-business-days?date=2026-03-25&office=mumbai&n=4"))

    # 26 Mar Ram Navami, 28 Mar 4th Sat, 29 Mar Sun, 31 Mar Mahavir Jayanti, 1 Apr closing.
    assert parsed["data"]["dates"] == ["2026-03-27", "2026-03-30", "2026-04-02", "2026-04-04"]
    assert parsed["data"]["n"] == 4
    assert parsed["meta"]["count"] == 4


def test_next_business_days_defaults_to_one(client: TestClient) -> None:
    parsed = body(client.get("/v1/calendar/next-business-days?date=2026-03-27&office=mumbai"))

    assert parsed["data"]["dates"] == ["2026-03-30"]


@pytest.mark.parametrize("n", [0, 32, -1, "x"])
def test_next_business_days_n_bounds(client: TestClient, n: object) -> None:
    assert_error(
        client.get(f"/v1/calendar/next-business-days?date=2026-03-27&office=mumbai&n={n}"),
        422,
        "INVALID_REQUEST",
    )


def test_next_business_days_runs_off_the_loaded_year(client: TestClient) -> None:
    assert_error(
        client.get("/v1/calendar/next-business-days?date=2026-12-30&office=mumbai&n=3"),
        409,
        "CALENDAR_DATA_MISSING",
    )


# ---------------------------------------------------------------- ICS
def _unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_ics_is_a_valid_calendar(client: TestClient) -> None:
    response = client.get("/v1/calendar/mumbai.ics?year=2026")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    raw = response.content
    assert raw.endswith(b"\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    lines = _unfold(response.text)[:-1]
    assert lines[:5] == [
        "BEGIN:VCALENDAR",
        "PRODID:-//India Merchant Data API//RBI bank holidays//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:RBI bank holidays: Mumbai 2026",
    ]
    assert lines[-1] == "END:VCALENDAR"
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 22
    for physical in response.text.split("\r\n"):
        assert len(physical.encode()) <= 75


def test_ics_event_for_republic_day(client: TestClient) -> None:
    lines = _unfold(client.get("/v1/calendar/mumbai.ics?year=2026").text)

    assert "UID:20260126-mumbai-ni_act@imda" in lines
    assert "DTSTART;VALUE=DATE:20260126" in lines
    assert "DTEND;VALUE=DATE:20260127" in lines
    assert "SUMMARY:Republic Day" in lines
    description = next(ln for ln in lines if ln.startswith("DESCRIPTION:") and "Republic" not in ln)
    assert "Source: RBI" in description
    assert "ni_act" in description
    assert "DTSTAMP:20260925T040000Z" in lines


def test_ics_escapes_commas_and_slashes_stay_literal(client: TestClient) -> None:
    text = _unfold(client.get("/v1/calendar/mumbai.ics?year=2026").text)
    summaries = [ln for ln in text if ln.startswith("SUMMARY:")]

    # Names with a comma must carry "\," and no bare comma may remain in any SUMMARY.
    assert all(not re.search(r"(?<!\\),", s) for s in summaries)


def test_ics_uids_are_stable_across_requests_and_unique(client: TestClient) -> None:
    first = client.get("/v1/calendar/mumbai.ics?year=2026").text
    second = client.get("/v1/calendar/mumbai.ics?year=2026").text

    assert first == second
    uids = re.findall(r"^UID:(.+)$", first, flags=re.M)
    assert len(uids) == len(set(uids)) == 22


def test_ics_weekly_off_adds_2nd_and_4th_saturdays(client: TestClient) -> None:
    plain = client.get("/v1/calendar/mumbai.ics?year=2026").text
    full = client.get("/v1/calendar/mumbai.ics?year=2026&include_weekly_off=true").text

    extra = full.count("BEGIN:VEVENT") - plain.count("BEGIN:VEVENT")
    assert extra == len(weekly_off_days(2026)) == 24
    assert "UID:20260328-mumbai-weekly_off@imda" in full
    assert "SUMMARY:4th Saturday" in full
    assert "UID:20260314-mumbai-weekly_off@imda" in full


def test_ics_errors_are_json(client: TestClient) -> None:
    assert_error(client.get("/v1/calendar/nowhere.ics?year=2026"), 404, "OFFICE_NOT_FOUND")
    assert_error(client.get("/v1/calendar/mumbai.ics?year=2027"), 409, "CALENDAR_DATA_MISSING")
    assert_error(client.get("/v1/calendar/mumbai.ics"), 422, "INVALID_REQUEST")
