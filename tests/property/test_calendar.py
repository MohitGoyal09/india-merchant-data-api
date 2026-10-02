"""Properties of the business-day engine over random synthetic holiday sets."""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from imda.domain.calendar import SATURDAY_RULE_START, HolidayCalendar
from imda.models import Holiday, HolidayKind
from tests.property.strategies import (
    LOADED_YEARS,
    calendars,
    office_slugs,
    query_dates,
)

DAY = dt.timedelta(days=1)
steps = st.integers(min_value=0, max_value=25)
_SATURDAY = 5


def _ordinal(day: dt.date) -> int:
    return (day.day - 1) // 7 + 1


@given(calendars(), office_slugs, query_dates, st.integers(min_value=1, max_value=25))
def test_add_business_days_lands_on_a_later_business_day(
    calendar: HolidayCalendar, office: str, day: dt.date, n: int
) -> None:
    result = calendar.add_business_days(office, day, n)

    assert calendar.is_business_day(office, result)
    assert result > day


@given(calendars(), office_slugs, query_dates, st.integers(min_value=1, max_value=25))
def test_add_business_days_is_strictly_monotonic_in_n(
    calendar: HolidayCalendar, office: str, day: dt.date, n: int
) -> None:
    assert calendar.add_business_days(office, day, n) < calendar.add_business_days(
        office, day, n + 1
    )


@given(calendars(), office_slugs, query_dates, st.integers(min_value=0, max_value=25))
def test_add_business_days_is_non_decreasing_from_zero(
    calendar: HolidayCalendar, office: str, day: dt.date, n: int
) -> None:
    # n == 0 rolls a non-working start forward, so it may equal n == 1 (documented).
    assert calendar.add_business_days(office, day, n) <= calendar.add_business_days(
        office, day, n + 1
    )


@given(calendars(), office_slugs, query_dates, st.integers(min_value=1, max_value=25))
def test_add_business_days_counts_exactly_n_business_days(
    calendar: HolidayCalendar, office: str, day: dt.date, n: int
) -> None:
    result = calendar.add_business_days(office, day, n)

    counted = calendar.business_days_between(office, day + DAY, result)
    assert len(counted) == n
    assert counted[-1] == result


@given(calendars(), office_slugs, query_dates)
def test_add_zero_business_days_is_the_first_business_day_on_or_after(
    calendar: HolidayCalendar, office: str, day: dt.date
) -> None:
    result = calendar.add_business_days(office, day, 0)

    assert result >= day
    assert calendar.is_business_day(office, result)
    assert not calendar.business_days_between(office, day, result)[:-1]


@given(calendars(), office_slugs, query_dates, st.booleans())
def test_next_business_day_is_a_business_day_not_before_the_start(
    calendar: HolidayCalendar, office: str, day: dt.date, include_start: bool
) -> None:
    result = calendar.next_business_day(office, day, include_start=include_start)

    assert calendar.is_business_day(office, result)
    assert result >= day
    assert include_start or result > day
    skipped = calendar.business_days_between(office, day if include_start else day + DAY, result)
    assert skipped == [result]


@given(calendars(), office_slugs, query_dates)
def test_non_working_reason_is_none_iff_business_day(
    calendar: HolidayCalendar, office: str, day: dt.date
) -> None:
    reason = calendar.non_working_reason(office, day)

    assert (reason is None) == calendar.is_business_day(office, day)
    assert reason is None or reason.strip()


@given(calendars(), office_slugs, query_dates)
def test_sundays_and_2nd_4th_saturdays_are_never_working(
    calendar: HolidayCalendar, office: str, day: dt.date
) -> None:
    weekday = day.weekday()
    if weekday == 6:
        assert not calendar.is_business_day(office, day)
    if weekday == _SATURDAY and _ordinal(day) in (2, 4):
        assert day >= SATURDAY_RULE_START
        assert not calendar.is_business_day(office, day)


@given(office_slugs, query_dates)
def test_1st_3rd_5th_saturdays_without_holiday_are_working(office: str, day: dt.date) -> None:
    empty = HolidayCalendar(
        [], closing_of_accounts_is_holiday=True, loaded_years={office: LOADED_YEARS}
    )
    if day.weekday() == _SATURDAY and _ordinal(day) in (1, 3, 5):
        assert empty.is_business_day(office, day)


@given(office_slugs, st.dates(min_value=dt.date(2015, 1, 1), max_value=dt.date(2015, 8, 31)))
def test_saturdays_before_the_rule_start_are_working(office: str, day: dt.date) -> None:
    empty = HolidayCalendar(
        [], closing_of_accounts_is_holiday=True, loaded_years={office: LOADED_YEARS}
    )
    assert empty.is_business_day(office, day) == (day.weekday() != 6)


@given(office_slugs, query_dates, st.sampled_from(list(HolidayKind)), st.booleans())
def test_listed_holiday_closes_the_office_according_to_its_kind(
    office: str, day: dt.date, kind: HolidayKind, closing_counts: bool
) -> None:
    loaded = {office: LOADED_YEARS}
    bare = HolidayCalendar([], closing_of_accounts_is_holiday=True, loaded_years=loaded)
    listed = HolidayCalendar(
        [Holiday(office_slug=office, date=day, name="Synthetic", kind=kind)],
        closing_of_accounts_is_holiday=closing_counts,
        loaded_years=loaded,
    )
    closes = kind is HolidayKind.NI_ACT or closing_counts

    expected = bare.is_business_day(office, day) and not closes
    assert listed.is_business_day(office, day) == expected
