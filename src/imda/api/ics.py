"""RFC 5545 iCalendar rendering for bank-holiday feeds. Pure functions, no I/O."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from imda.domain.calendar import SATURDAY_RULE_START
from imda.models import Holiday, HolidayKind, Office

PRODID = "-//India Merchant Data API//RBI bank holidays//EN"
CRLF = "\r\n"
MAX_OCTETS = 75
WEEKLY_OFF_KIND = "weekly_off"
_SATURDAY = 5
_DAYS_PER_WEEK = 7
_SATURDAY_ORDINALS = {2: "2nd Saturday", 4: "4th Saturday"}
_KIND_LABELS = {
    HolidayKind.NI_ACT.value: "Holiday under the Negotiable Instruments Act (banks closed)",
    HolidayKind.CLOSING_OF_ACCOUNTS.value: "Banks' closing of accounts",
    WEEKLY_OFF_KIND: "Weekly off: banks closed on the 2nd and 4th Saturday",
}


def escape_text(value: str) -> str:
    """Escape a TEXT value: backslash, semicolon, comma and newlines."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_line(line: str) -> list[str]:
    """Split ``line`` into physical lines of at most 75 octets, never inside a character.

    Continuation lines start with one space, which counts toward their 75 octets.
    """
    physical: list[str] = []
    current: list[str] = []
    used = 0
    for char in line:
        size = len(char.encode("utf-8"))
        limit = MAX_OCTETS if not physical else MAX_OCTETS - 1
        if used + size > limit:
            physical.append("".join(current))
            current, used = [char], size
        else:
            current.append(char)
            used += size
    physical.append("".join(current))
    return [physical[0], *(" " + part for part in physical[1:])]


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _uid(day: dt.date, office: str, kind: str) -> str:
    return f"{day:%Y%m%d}-{office}-{kind}@imda"


def weekly_off_days(year: int) -> list[dt.date]:
    """The 2nd and 4th Saturdays of ``year`` (the rule applies from 2015-09-01)."""
    first, last = dt.date(year, 1, 1), dt.date(year, 12, 31)
    days = (first + dt.timedelta(days=i) for i in range((last - first).days + 1))
    return [
        d
        for d in days
        if d.weekday() == _SATURDAY
        and d >= SATURDAY_RULE_START
        and (d.day - 1) // _DAYS_PER_WEEK + 1 in _SATURDAY_ORDINALS
    ]


def _event(
    day: dt.date, office: str, kind: str, summary: str, description: str, stamp: str
) -> list[str]:
    return [
        "BEGIN:VEVENT",
        f"UID:{_uid(day, office, kind)}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
        f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{escape_text(summary)}",
        f"DESCRIPTION:{escape_text(description)}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


def render_calendar(
    office: Office,
    year: int,
    holidays: Iterable[Holiday],
    *,
    stamp: dt.datetime,
    include_weekly_off: bool = False,
) -> str:
    """One all-day event per holiday. CRLF line endings, lines folded at 75 octets."""
    dtstamp = _stamp(stamp)
    lines = [
        "BEGIN:VCALENDAR",
        f"PRODID:{PRODID}",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{escape_text(f'RBI bank holidays: {office.name} {year}')}",
    ]
    events: list[tuple[dt.date, str, list[str]]] = []
    for holiday in holidays:
        kind = holiday.kind.value
        description = f"{_KIND_LABELS[kind]}\nKind: {kind}\nSource: RBI"
        events.append(
            (
                holiday.date,
                kind,
                _event(holiday.date, office.slug, kind, holiday.name, description, dtstamp),
            )
        )
    if include_weekly_off:
        for day in weekly_off_days(year):
            ordinal = _SATURDAY_ORDINALS[(day.day - 1) // _DAYS_PER_WEEK + 1]
            description = f"{_KIND_LABELS[WEEKLY_OFF_KIND]}\nKind: {WEEKLY_OFF_KIND}\nSource: RBI"
            events.append(
                (
                    day,
                    WEEKLY_OFF_KIND,
                    _event(day, office.slug, WEEKLY_OFF_KIND, ordinal, description, dtstamp),
                )
            )
    for _, _, event in sorted(events, key=lambda e: (e[0], e[1])):
        lines.extend(event)
    lines.append("END:VCALENDAR")
    return CRLF.join(part for line in lines for part in fold_line(line)) + CRLF
