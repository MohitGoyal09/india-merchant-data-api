"""Table-driven tests for the holiday calendar (synthetic holidays only)."""

from __future__ import annotations

import datetime as dt

import pytest

from imda.domain.calendar import (
    CalendarDataMissing,
    HolidayCalendar,
    SkippedDay,
)
from imda.models import Holiday, HolidayKind

D = dt.date
MUM = "mumbai"


def hol(
    day: dt.date, name: str, kind: HolidayKind = HolidayKind.NI_ACT, office: str = MUM
) -> Holiday:
    return Holiday(office_slug=office, date=day, name=name, kind=kind)


def cal(
    holidays: list[Holiday] | None = None,
    years: tuple[int, ...] = (2015, 2019, 2025, 2026, 2027),
    *,
    closing: bool = True,
) -> HolidayCalendar:
    return HolidayCalendar(
        holidays or [],
        closing_of_accounts_is_holiday=closing,
        loaded_years={MUM: frozenset(years)},
    )


class TestNonWorkingReason:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (D(2026, 3, 29), "Sunday"),  # Sunday
            (D(2026, 3, 7), None),  # 1st Saturday
            (D(2026, 3, 14), "2nd Saturday"),
            (D(2026, 3, 21), None),  # 3rd Saturday
            (D(2026, 3, 28), "4th Saturday"),
            (D(2026, 5, 30), None),  # 5th Saturday
            (D(2026, 3, 27), None),  # plain Friday
            (D(2015, 8, 8), None),  # 2nd Saturday before the rule start
            (D(2015, 8, 22), None),  # 4th Saturday before the rule start
            (D(2015, 8, 30), "Sunday"),  # Sundays always count
            (D(2015, 9, 12), "2nd Saturday"),  # first 2nd Saturday on the rule
            (D(2015, 9, 26), "4th Saturday"),
        ],
    )
    def test_weekend_rules(self, day: dt.date, expected: str | None) -> None:
        assert cal().non_working_reason(MUM, day) == expected

    def test_ni_act_holiday_uses_its_name(self) -> None:
        c = cal([hol(D(2026, 3, 31), "Mahavir Jayanti")])
        assert c.non_working_reason(MUM, D(2026, 3, 31)) == "Mahavir Jayanti"

    @pytest.mark.parametrize(
        ("closing", "expected"),
        [(True, "Annual Closing (closing of accounts)"), (False, None)],
    )
    def test_closing_of_accounts_toggle(self, closing: bool, expected: str | None) -> None:
        c = cal(
            [hol(D(2026, 4, 1), "Annual Closing", HolidayKind.CLOSING_OF_ACCOUNTS)],
            closing=closing,
        )
        assert c.non_working_reason(MUM, D(2026, 4, 1)) == expected

    def test_reasons_are_joined_in_rule_order(self) -> None:
        # 2026-01-26 is a Monday; 2027-01-26 a Tuesday. Use a Sunday holiday.
        c = cal([hol(D(2026, 3, 29), "Gudi Padwa")])
        assert c.non_working_reason(MUM, D(2026, 3, 29)) == "Sunday; Gudi Padwa"

    def test_saturday_and_holiday_and_closing_all_join(self) -> None:
        day = D(2026, 3, 28)
        c = cal(
            [
                hol(day, "Closing", HolidayKind.CLOSING_OF_ACCOUNTS),
                hol(day, "Festival"),
            ]
        )
        assert (
            c.non_working_reason(MUM, day)
            == "4th Saturday; Festival; Closing (closing of accounts)"
        )

    def test_holiday_of_other_office_is_ignored(self) -> None:
        c = HolidayCalendar(
            [hol(D(2026, 3, 31), "Elsewhere", office="delhi")],
            closing_of_accounts_is_holiday=True,
            loaded_years={MUM: frozenset({2026}), "delhi": frozenset({2026})},
        )
        assert c.non_working_reason(MUM, D(2026, 3, 31)) is None
        assert c.non_working_reason("delhi", D(2026, 3, 31)) == "Elsewhere"

    def test_year_loaded_with_zero_holidays_is_fine(self) -> None:
        assert cal(years=(2019,)).is_business_day(MUM, D(2019, 2, 4)) is True


class TestMissingData:
    def test_unloaded_year_raises(self) -> None:
        with pytest.raises(CalendarDataMissing) as exc:
            cal(years=(2026,)).is_business_day(MUM, D(2027, 1, 4))
        assert (exc.value.office, exc.value.year) == (MUM, 2027)

    def test_unknown_office_raises(self) -> None:
        with pytest.raises(CalendarDataMissing) as exc:
            cal().non_working_reason("atlantis", D(2026, 3, 3))
        assert (exc.value.office, exc.value.year) == ("atlantis", 2026)

    def test_sunday_in_unloaded_year_still_raises(self) -> None:
        with pytest.raises(CalendarDataMissing):
            cal(years=(2026,)).non_working_reason(MUM, D(2030, 1, 6))

    def test_year_boundary_needs_next_year_loaded(self) -> None:
        # Thu 2026-12-31 -> next business day is Fri 2027-01-01 (year 2027 missing).
        with pytest.raises(CalendarDataMissing) as exc:
            cal(years=(2026,)).next_business_day(MUM, D(2026, 12, 31))
        assert exc.value.year == 2027


class TestNavigation:
    def test_next_business_day_skips_weekend(self) -> None:
        assert cal().next_business_day(MUM, D(2026, 3, 27)) == D(2026, 3, 30)

    @pytest.mark.parametrize(
        ("day", "include_start", "expected"),
        [
            (D(2026, 3, 30), False, D(2026, 3, 31)),
            (D(2026, 3, 30), True, D(2026, 3, 30)),
            (D(2026, 3, 29), True, D(2026, 3, 30)),  # Sunday start rolls forward
        ],
    )
    def test_include_start(self, day: dt.date, include_start: bool, expected: dt.date) -> None:
        assert cal().next_business_day(MUM, day, include_start=include_start) == expected

    def test_year_boundary_rolls_into_january(self) -> None:
        c = cal([hol(D(2027, 1, 1), "New Year")], years=(2026, 2027))
        # Thu 2026-12-31 -> Fri 2027-01-01 is a holiday; Sat 2027-01-02 is a 1st Saturday.
        assert c.next_business_day(MUM, D(2026, 12, 31)) == D(2027, 1, 2)

    @pytest.mark.parametrize(
        ("day", "n", "expected"),
        [
            (D(2026, 3, 30), 0, D(2026, 3, 30)),
            (D(2026, 3, 29), 0, D(2026, 3, 30)),  # n=0 on a Sunday
            (D(2026, 3, 27), 1, D(2026, 3, 30)),
            (D(2026, 3, 27), 2, D(2026, 3, 31)),
            (D(2026, 3, 27), 5, D(2026, 4, 3)),
        ],
    )
    def test_add_business_days(self, day: dt.date, n: int, expected: dt.date) -> None:
        assert cal().add_business_days(MUM, day, n) == expected

    def test_next_business_day_guard_when_every_day_is_a_holiday(self) -> None:
        start = D(2026, 1, 1)
        every_day = [hol(start + dt.timedelta(days=i), "Closed") for i in range(400)]
        c = cal(every_day, years=(2026, 2027))
        with pytest.raises(ValueError, match="366"):
            c.next_business_day(MUM, start)

    def test_add_business_days_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="n must be"):
            cal().add_business_days(MUM, D(2026, 3, 27), -1)

    def test_iteration_guard(self) -> None:
        with pytest.raises(ValueError, match="366"):
            cal().add_business_days(MUM, D(2026, 3, 2), 400)

    def test_business_days_between_is_inclusive(self) -> None:
        got = cal().business_days_between(MUM, D(2026, 3, 27), D(2026, 4, 1))
        assert got == [D(2026, 3, 27), D(2026, 3, 30), D(2026, 3, 31), D(2026, 4, 1)]

    def test_skipped_days_reports_reasons(self) -> None:
        got = cal().skipped_days(MUM, D(2026, 3, 27), D(2026, 3, 30))
        assert got == [
            SkippedDay(D(2026, 3, 28), "4th Saturday"),
            SkippedDay(D(2026, 3, 29), "Sunday"),
        ]

    @pytest.mark.parametrize("method", ["business_days_between", "skipped_days"])
    def test_range_errors(self, method: str) -> None:
        fn = getattr(cal(), method)
        with pytest.raises(ValueError, match="before start"):
            fn(MUM, D(2026, 3, 30), D(2026, 3, 27))
        with pytest.raises(ValueError, match="366"):
            fn(MUM, D(2025, 1, 1), D(2026, 12, 31))


class TestImmutability:
    def test_cannot_set_attributes(self) -> None:
        with pytest.raises(AttributeError):
            cal().extra = 1  # type: ignore[attr-defined]

    def test_input_mutation_does_not_leak(self) -> None:
        holidays = [hol(D(2026, 3, 31), "Festival")]
        years = {MUM: frozenset({2026})}
        c = HolidayCalendar(holidays, closing_of_accounts_is_holiday=True, loaded_years=years)
        holidays.clear()
        years.clear()
        assert c.non_working_reason(MUM, D(2026, 3, 31)) == "Festival"
