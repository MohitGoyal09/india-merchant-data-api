"""Shared Hypothesis strategies for the property suite."""

from __future__ import annotations

import datetime as dt

from hypothesis import strategies as st

from imda.domain.calendar import HolidayCalendar
from imda.models import Holiday, HolidayKind

OFFICES = ("mumbai", "chennai", "kolkata")
# Years with holiday data loaded. The 2015 and 2031 margins let searches run across the
# edges of the 2016-2030 query window without raising CalendarDataMissing.
LOADED_YEARS = frozenset(range(2015, 2032))
QUERY_MIN = dt.date(2016, 1, 1)
QUERY_MAX = dt.date(2030, 12, 31)
MAX_HOLIDAYS = 40

office_slugs = st.sampled_from(OFFICES)
query_dates = st.dates(min_value=QUERY_MIN, max_value=QUERY_MAX)
holiday_kinds = st.sampled_from(list(HolidayKind))


@st.composite
def calendars(draw: st.DrawFn) -> HolidayCalendar:
    """A calendar with random holidays per office, spread over 2016-2030."""
    holidays = [
        Holiday(office_slug=office, date=day, name=f"Holiday {day.isoformat()}", kind=kind)
        for office in OFFICES
        for day, kind in draw(
            st.dictionaries(query_dates, holiday_kinds, max_size=MAX_HOLIDAYS)
        ).items()
    ]
    return HolidayCalendar(
        holidays,
        closing_of_accounts_is_holiday=draw(st.booleans()),
        loaded_years={office: LOADED_YEARS for office in OFFICES},
    )
