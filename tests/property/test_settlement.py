"""Properties of the settlement ETA over random timestamps, offsets and cycles."""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from imda.domain.calendar import HolidayCalendar
from imda.domain.settlement import SettlementMode, estimate_settlement
from imda.models import IST
from tests.property.strategies import calendars, office_slugs

DAY = dt.timedelta(days=1)
cycles = st.integers(min_value=0, max_value=10)
modes = st.sampled_from(list(SettlementMode))
offsets = st.integers(min_value=-12 * 4, max_value=14 * 4).map(
    lambda quarters: dt.timezone(dt.timedelta(minutes=15 * quarters))
)
# Keep the IST date inside 2016-2030 for any offset so every touched year has data.
captured = st.datetimes(
    min_value=dt.datetime(2016, 1, 3), max_value=dt.datetime(2030, 11, 30), timezones=offsets
)


@given(calendars(), office_slugs, captured, cycles, modes)
def test_eta_is_a_business_day_on_or_after_the_ist_capture_date(
    calendar: HolidayCalendar,
    office: str,
    moment: dt.datetime,
    cycle: int,
    mode: SettlementMode,
) -> None:
    estimate = estimate_settlement(moment, office, cycle, calendar, mode)

    assert estimate.capture_date == moment.astimezone(IST).date()
    assert calendar.is_business_day(office, estimate.eta_date)
    assert estimate.eta_date >= estimate.capture_date


@given(calendars(), office_slugs, captured, st.integers(min_value=0, max_value=9))
def test_a_larger_cycle_never_gives_an_earlier_eta(
    calendar: HolidayCalendar, office: str, moment: dt.datetime, cycle: int
) -> None:
    shorter = estimate_settlement(moment, office, cycle, calendar, SettlementMode.WORKING_DAYS)
    longer = estimate_settlement(moment, office, cycle + 1, calendar, SettlementMode.WORKING_DAYS)

    assert longer.eta_date >= shorter.eta_date


@given(calendars(), office_slugs, captured, st.integers(min_value=0, max_value=9))
def test_a_larger_calendar_then_roll_cycle_never_gives_an_earlier_eta(
    calendar: HolidayCalendar, office: str, moment: dt.datetime, cycle: int
) -> None:
    mode = SettlementMode.CALENDAR_THEN_ROLL
    shorter = estimate_settlement(moment, office, cycle, calendar, mode)
    longer = estimate_settlement(moment, office, cycle + 1, calendar, mode)

    assert longer.eta_date >= shorter.eta_date


@given(calendars(), office_slugs, captured, cycles, modes)
def test_skipped_days_are_non_working_and_strictly_between_capture_and_eta(
    calendar: HolidayCalendar,
    office: str,
    moment: dt.datetime,
    cycle: int,
    mode: SettlementMode,
) -> None:
    estimate = estimate_settlement(moment, office, cycle, calendar, mode)

    for skipped in estimate.skipped:
        assert estimate.capture_date < skipped.date < estimate.eta_date
        assert not calendar.is_business_day(office, skipped.date)
        assert skipped.reason == calendar.non_working_reason(office, skipped.date)
    # Together with the business days in between, the skipped days cover the whole gap.
    gap = (estimate.eta_date - estimate.capture_date).days - 1
    business_between = (
        calendar.business_days_between(office, estimate.capture_date + DAY, estimate.eta_date - DAY)
        if gap > 0
        else []
    )
    assert len(estimate.skipped) + len(business_between) == max(gap, 0)


@given(calendars(), office_slugs, captured, cycles)
def test_working_days_mode_counts_exactly_cycle_days(
    calendar: HolidayCalendar, office: str, moment: dt.datetime, cycle: int
) -> None:
    estimate = estimate_settlement(moment, office, cycle, calendar, SettlementMode.WORKING_DAYS)

    assert len(estimate.counted_days) == cycle
    assert all(calendar.is_business_day(office, d) for d in estimate.counted_days)
    assert estimate.counted_days == sorted(set(estimate.counted_days))
    if cycle:
        assert estimate.counted_days[-1] == estimate.eta_date
        assert estimate.counted_days[0] > estimate.capture_date


@given(calendars(), office_slugs, captured, cycles)
def test_calendar_then_roll_is_the_first_business_day_from_the_calendar_target(
    calendar: HolidayCalendar, office: str, moment: dt.datetime, cycle: int
) -> None:
    estimate = estimate_settlement(
        moment, office, cycle, calendar, SettlementMode.CALENDAR_THEN_ROLL
    )

    target = estimate.capture_date + dt.timedelta(days=cycle)
    assert estimate.counted_days == []
    assert estimate.eta_date >= target
    assert not calendar.business_days_between(office, target, estimate.eta_date)[:-1]
