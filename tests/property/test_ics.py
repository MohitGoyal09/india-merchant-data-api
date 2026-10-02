"""Properties of iCalendar escaping and folding for arbitrary holiday names."""

from __future__ import annotations

import datetime as dt
import re

from hypothesis import given
from hypothesis import strategies as st

from imda.api.ics import _STRIPPED, CRLF, MAX_OCTETS, render_calendar
from imda.models import Holiday, HolidayKind, Office

STAMP = dt.datetime(2026, 9, 25, 4, 0, tzinfo=dt.UTC)
SUSPECT_CODEPOINTS = (
    0x0D, 0x0A, 0x3B, 0x2C, 0x5C, 0x00, 0x0B, 0x0C, 0x7F, 0x85, 0x2028, 0x2029, 0x09, 0x20,
    0xE9, 0x1F389,
)  # fmt: skip
SUSPECTS = "".join(map(chr, SUSPECT_CODEPOINTS))
names = st.text(
    alphabet=st.one_of(st.characters(codec="utf-8"), st.sampled_from(SUSPECTS)), max_size=200
)
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)


def office(name: str) -> Office:
    return Office(rbi_id=1, slug="mumbai", name=name)


def holiday(name: str, day: dt.date) -> Holiday:
    return Holiday(office_slug="mumbai", date=day, name=name, kind=HolidayKind.NI_ACT)


def unfold(body: str) -> list[str]:
    """Logical content lines: split on CRLF, then join continuation lines (leading space)."""
    logical: list[str] = []
    for physical in body.split(CRLF):
        if physical.startswith(" ") and logical:
            logical[-1] += physical[1:]
        else:
            logical.append(physical)
    return logical


def unescape(value: str) -> str:
    return _ESCAPE.sub(lambda m: "\n" if m.group(1) == "n" else m.group(1), value)


def expected_text(name: str) -> str:
    """The name as a TEXT value should read back: controls stripped, line breaks as LF."""
    stripped = _STRIPPED.sub("", name)
    return stripped.replace("\r\n", "\n").replace("\r", "\n")


@given(names, names)
def test_rendered_calendar_has_only_crlf_breaks_and_short_lines(
    holiday_name: str, office_name: str
) -> None:
    body = render_calendar(
        office(office_name), 2026, [holiday(holiday_name, dt.date(2026, 3, 31))], stamp=STAMP
    )

    assert body.endswith(CRLF)
    for physical in body.split(CRLF)[:-1]:
        assert "\r" not in physical
        assert "\n" not in physical
        assert len(physical.encode("utf-8")) <= MAX_OCTETS
        assert physical  # no blank line that could end a component early


@given(names)
def test_unfolding_and_unescaping_recovers_the_sanitised_name(holiday_name: str) -> None:
    body = render_calendar(
        office("Mumbai"), 2026, [holiday(holiday_name, dt.date(2026, 3, 31))], stamp=STAMP
    )

    summaries = [line for line in unfold(body) if line.startswith("SUMMARY:")]
    assert len(summaries) == 1
    assert unescape(summaries[0].removeprefix("SUMMARY:")) == expected_text(holiday_name)


@given(names, st.integers(min_value=0, max_value=5))
def test_a_hostile_name_cannot_inject_properties_or_events(holiday_name: str, extra: int) -> None:
    days = [dt.date(2026, 3, 1) + dt.timedelta(days=i) for i in range(extra + 1)]
    body = render_calendar(
        office("Mumbai"), 2026, [holiday(holiday_name, d) for d in days], stamp=STAMP
    )

    lines = unfold(body)[:-1]
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == len(days)
    assert lines[0] == "BEGIN:VCALENDAR"
    assert lines[-1] == "END:VCALENDAR"
    allowed = (
        "BEGIN:", "END:", "PRODID:", "VERSION:", "CALSCALE:", "X-WR-CALNAME:", "UID:",
        "DTSTAMP:", "DTSTART;", "DTEND;", "SUMMARY:", "DESCRIPTION:", "TRANSP:",
    )  # fmt: skip
    assert all(line.startswith(allowed) for line in lines)
