"""Unit tests for the RFC 5545 helpers."""

from __future__ import annotations

import datetime as dt

import pytest

from imda.api.ics import escape_text, fold_line, render_calendar, weekly_off_days
from imda.models import Holiday, HolidayKind, Office

OFFICE = Office(rbi_id=28, slug="mumbai", name="Mumbai", state="Maharashtra")
STAMP = dt.datetime(2026, 9, 25, 4, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("a,b", r"a\,b"),
        ("a;b", r"a\;b"),
        ("a\\b", r"a\\b"),
        ("a\nb", r"a\nb"),
        ("a\r\nb", r"a\nb"),
        (r"\;,", r"\\\;\,"),
        ("plain", "plain"),
    ],
)
def test_escape_text(raw: str, escaped: str) -> None:
    assert escape_text(raw) == escaped


def test_fold_short_line_is_untouched() -> None:
    assert fold_line("SUMMARY:x") == ["SUMMARY:x"]


def test_fold_ascii_at_75_octets_and_unfolds_losslessly() -> None:
    line = "SUMMARY:" + "x" * 200

    parts = fold_line(line)

    assert all(len(p.encode()) <= 75 for p in parts)
    assert len(parts[0]) == 75
    assert all(p.startswith(" ") for p in parts[1:])
    assert "".join([parts[0], *(p[1:] for p in parts[1:])]) == line


def test_fold_never_splits_a_multibyte_character() -> None:
    line = "SUMMARY:" + "भारत" * 40

    parts = fold_line(line)

    assert all(len(p.encode()) <= 75 for p in parts)
    for part in parts:
        part.encode("utf-8").decode("utf-8")
    assert "".join([parts[0], *(p[1:] for p in parts[1:])]) == line


def test_fold_exact_boundary() -> None:
    assert len(fold_line("a" * 75)) == 1
    assert len(fold_line("a" * 76)) == 2


def test_render_uses_crlf_and_one_event_per_holiday() -> None:
    holidays = [
        Holiday(
            office_slug="mumbai",
            date=dt.date(2026, 1, 26),
            name="Republic, Day; \\x",
            kind=HolidayKind.NI_ACT,
        ),
        Holiday(
            office_slug="mumbai",
            date=dt.date(2026, 4, 1),
            name="Closing",
            kind=HolidayKind.CLOSING_OF_ACCOUNTS,
        ),
    ]

    text = render_calendar(OFFICE, 2026, holidays, stamp=STAMP)

    assert text.endswith("END:VCALENDAR\r\n")
    assert text.count("BEGIN:VEVENT") == 2
    assert r"SUMMARY:Republic\, Day\; \\x" in text
    assert "UID:20260401-mumbai-closing_of_accounts@imda" in text


def test_weekly_off_respects_the_2015_rule_start() -> None:
    assert weekly_off_days(2015)[0] == dt.date(2015, 9, 12)
    assert len(weekly_off_days(2026)) == 24
