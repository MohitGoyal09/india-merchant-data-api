"""Table-driven tests for settlement ETA estimation (synthetic holidays only)."""

from __future__ import annotations

import datetime as dt

import pytest

from imda.domain.calendar import CalendarDataMissing, HolidayCalendar, SkippedDay
from imda.domain.settlement import SettlementMode, estimate_settlement
from imda.errors import InvalidInput
from imda.models import IST, Holiday, HolidayKind

D = dt.date
MUM = "mumbai"


def mumbai_2026(*, closing: bool = True) -> HolidayCalendar:
    return HolidayCalendar(
        [
            Holiday(
                office_slug=MUM,
                date=D(2026, 3, 31),
                name="Mahavir Jayanti",
                kind=HolidayKind.NI_ACT,
            ),
            Holiday(
                office_slug=MUM,
                date=D(2026, 4, 1),
                name="Annual Closing",
                kind=HolidayKind.CLOSING_OF_ACCOUNTS,
            ),
        ],
        closing_of_accounts_is_holiday=closing,
        loaded_years={MUM: frozenset({2026})},
    )


def empty_2019() -> HolidayCalendar:
    return HolidayCalendar(
        [], closing_of_accounts_is_holiday=True, loaded_years={MUM: frozenset({2019})}
    )


def ist(y: int, m: int, d: int, hh: int = 12) -> dt.datetime:
    return dt.datetime(y, m, d, hh, tzinfo=IST)


class TestRazorpayModes:
    def test_calendar_then_roll_reproduces_docs_example(self) -> None:
        est = estimate_settlement(
            ist(2019, 2, 2), MUM, 2, empty_2019(), SettlementMode.CALENDAR_THEN_ROLL
        )
        assert est.eta_date == D(2019, 2, 4)
        assert est.counted_days == []
        assert est.skipped == [SkippedDay(D(2019, 2, 3), "Sunday")]

    def test_working_days_mode_is_the_default_and_differs(self) -> None:
        est = estimate_settlement(ist(2019, 2, 2), MUM, 2, empty_2019())
        assert est.mode is SettlementMode.WORKING_DAYS
        assert est.eta_date == D(2019, 2, 5)
        assert est.counted_days == [D(2019, 2, 4), D(2019, 2, 5)]
        assert est.skipped == [SkippedDay(D(2019, 2, 3), "Sunday")]

    def test_calendar_mode_rolls_forward_off_a_holiday(self) -> None:
        # Fri 2026-03-27 + 2 calendar days = Sun 29 -> Mon 30.
        est = estimate_settlement(
            ist(2026, 3, 27), MUM, 2, mumbai_2026(), SettlementMode.CALENDAR_THEN_ROLL
        )
        assert est.eta_date == D(2026, 3, 30)
        assert [s.date for s in est.skipped] == [D(2026, 3, 28), D(2026, 3, 29)]


class TestWorkingDays:
    def test_friday_capture_across_quarter_end_with_closing_of_accounts(self) -> None:
        est = estimate_settlement(ist(2026, 3, 27), MUM, 2, mumbai_2026())
        assert est.capture_date == D(2026, 3, 27)
        assert est.eta_date == D(2026, 4, 2)
        assert est.counted_days == [D(2026, 3, 30), D(2026, 4, 2)]
        assert est.skipped == [
            SkippedDay(D(2026, 3, 28), "4th Saturday"),
            SkippedDay(D(2026, 3, 29), "Sunday"),
            SkippedDay(D(2026, 3, 31), "Mahavir Jayanti"),
            SkippedDay(D(2026, 4, 1), "Annual Closing (closing of accounts)"),
        ]

    def test_closing_of_accounts_off_gives_earlier_eta(self) -> None:
        est = estimate_settlement(ist(2026, 3, 27), MUM, 2, mumbai_2026(closing=False))
        assert est.eta_date == D(2026, 4, 1)
        assert est.counted_days == [D(2026, 3, 30), D(2026, 4, 1)]

    def test_utc_timestamp_is_converted_to_ist_date(self) -> None:
        # 2026-03-27T20:00Z is 01:30 IST on Sat 2026-03-28.
        captured = dt.datetime(2026, 3, 27, 20, 0, tzinfo=dt.UTC)
        est = estimate_settlement(captured, MUM, 2, mumbai_2026())
        assert est.capture_date == D(2026, 3, 28)
        assert est.captured_at == captured
        # T = Sat 28 (4th Saturday); 29 Sun; 30 = T+1; 31, 1 Apr skipped; 2 Apr = T+2.
        assert est.eta_date == D(2026, 4, 2)
        assert est.counted_days == [D(2026, 3, 30), D(2026, 4, 2)]

    @pytest.mark.parametrize(
        ("captured", "expected"),
        [
            (ist(2026, 3, 30), D(2026, 3, 30)),  # business day: settles same day
            (ist(2026, 3, 29), D(2026, 3, 30)),  # Sunday: next business day
        ],
    )
    def test_zero_cycle(self, captured: dt.datetime, expected: dt.date) -> None:
        est = estimate_settlement(captured, MUM, 0, mumbai_2026())
        assert est.eta_date == expected
        assert est.counted_days == []

    def test_zero_cycle_on_business_day_skips_nothing(self) -> None:
        est = estimate_settlement(ist(2026, 3, 30), MUM, 0, mumbai_2026())
        assert est.skipped == []

    def test_zero_cycle_calendar_mode_on_sunday_rolls(self) -> None:
        est = estimate_settlement(
            ist(2026, 3, 29), MUM, 0, mumbai_2026(), SettlementMode.CALENDAR_THEN_ROLL
        )
        assert est.eta_date == D(2026, 3, 30)
        assert est.skipped == []

    def test_estimate_is_frozen(self) -> None:
        est = estimate_settlement(ist(2026, 3, 30), MUM, 2, mumbai_2026())
        with pytest.raises(AttributeError):
            est.eta_date = D(2026, 1, 1)  # type: ignore[misc]


class TestErrors:
    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(InvalidInput, match="timezone"):
            estimate_settlement(dt.datetime(2026, 3, 27, 12), MUM, 2, mumbai_2026())

    def test_negative_cycle_rejected(self) -> None:
        with pytest.raises(InvalidInput, match="cycle_days"):
            estimate_settlement(ist(2026, 3, 27), MUM, -1, mumbai_2026())

    def test_missing_year_raises(self) -> None:
        with pytest.raises(CalendarDataMissing) as exc:
            estimate_settlement(ist(2026, 12, 30), MUM, 2, mumbai_2026())
        assert exc.value.year == 2027

    def test_unknown_office_raises(self) -> None:
        with pytest.raises(CalendarDataMissing):
            estimate_settlement(ist(2026, 3, 27), "atlantis", 2, mumbai_2026())

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(InvalidInput, match="mode"):
            estimate_settlement(ist(2026, 3, 27), MUM, 2, mumbai_2026(), "bogus")  # type: ignore[arg-type]
