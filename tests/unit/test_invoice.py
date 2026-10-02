"""Tests for the invoice quote helper (fake reader + synthetic calendar)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from imda.config import Settings
from imda.domain.calendar import HolidayCalendar
from imda.domain.fx_service import FxService
from imda.domain.invoice import SETTLEMENT_NOTE, quote_invoice
from imda.domain.settlement import SettlementMode
from imda.models import IST, Currency, FxRate, Holiday, HolidayKind, Source

D = dt.date
MUM = "mumbai"


class FakeReader:
    def __init__(self, rows: list[FxRate]) -> None:
        self.rows = rows

    def fx_rates(
        self, currency: Currency, start: dt.date, end: dt.date, source: Source | None = None
    ) -> list[FxRate]:
        return [
            r
            for r in self.rows
            if r.currency == currency
            and start <= r.date <= end
            and (source is None or r.source == source)
        ]

    def latest_fx_date(self, currency: Currency, source: Source | None = None) -> dt.date | None:
        return max((r.date for r in self.rows if r.currency == currency), default=None)


def make_calendar() -> HolidayCalendar:
    holiday = Holiday(
        office_slug=MUM, date=D(2026, 1, 26), name="Republic Day", kind=HolidayKind.NI_ACT
    )
    return HolidayCalendar(
        [holiday], closing_of_accounts_is_holiday=True, loaded_years={MUM: frozenset({2026})}
    )


@pytest.fixture
def fx_and_calendar() -> tuple[FxService, HolidayCalendar]:
    calendar = make_calendar()
    rows = [
        FxRate(
            currency=Currency.USD,
            date=D(2026, 1, 23),
            rate=Decimal("90.00"),
            unit=1,
            source=Source.FBIL,
        ),
        FxRate(
            currency=Currency.USD,
            date=D(2026, 1, 27),
            rate=Decimal("91.00"),
            unit=1,
            source=Source.FBIL,
        ),
    ]
    fx = FxService(
        FakeReader(rows),
        calendar=calendar,
        settings=Settings(_env_file=None),
        now=lambda: dt.datetime(2026, 3, 10, 18, tzinfo=IST),
    )
    return fx, calendar


def test_quote_without_captured_at_has_no_settlement(
    fx_and_calendar: tuple[FxService, HolidayCalendar],
) -> None:
    fx, calendar = fx_and_calendar
    quote = quote_invoice(
        amount=Decimal("100.00"),
        currency=Currency.USD,
        invoice_date=D(2026, 1, 27),
        office=MUM,
        fx=fx,
        calendar=calendar,
        cycle_days=2,
    )
    assert quote.conversion.result == Decimal("9100.00")
    assert quote.settlement is None
    assert quote.notes == ()


def test_quote_on_holiday_notes_rate_date_and_adds_settlement(
    fx_and_calendar: tuple[FxService, HolidayCalendar],
) -> None:
    fx, calendar = fx_and_calendar
    quote = quote_invoice(
        amount=Decimal("100.00"),
        currency=Currency.USD,
        invoice_date=D(2026, 1, 26),
        office=MUM,
        fx=fx,
        calendar=calendar,
        captured_at=dt.datetime(2026, 1, 27, 11, tzinfo=IST),
        cycle_days=2,
    )
    assert quote.conversion.result == Decimal("9000.00")
    assert quote.conversion.rates_used[0].effective_date == D(2026, 1, 23)
    assert quote.settlement is not None
    assert quote.settlement.eta_date == D(2026, 1, 29)
    assert quote.notes == (
        "rate is from 2026-01-23 (Republic Day)",
        "settlement estimate is indicative, not Razorpay's settlement engine",
    )
    assert quote.notes[1] == SETTLEMENT_NOTE


def test_mode_is_passed_through(fx_and_calendar: tuple[FxService, HolidayCalendar]) -> None:
    fx, calendar = fx_and_calendar
    captured = dt.datetime(2026, 1, 24, 11, tzinfo=IST)  # Saturday (4th)
    working = quote_invoice(
        amount=Decimal("1"),
        currency=Currency.USD,
        invoice_date=D(2026, 1, 23),
        office=MUM,
        fx=fx,
        calendar=calendar,
        captured_at=captured,
        cycle_days=2,
    )
    rolled = quote_invoice(
        amount=Decimal("1"),
        currency=Currency.USD,
        invoice_date=D(2026, 1, 23),
        office=MUM,
        fx=fx,
        calendar=calendar,
        captured_at=captured,
        cycle_days=2,
        mode=SettlementMode.CALENDAR_THEN_ROLL,
        source="fbil",
    )
    assert working.settlement is not None
    assert rolled.settlement is not None
    assert working.settlement.eta_date == D(2026, 1, 28)  # skips the Republic Day Monday
    assert rolled.settlement.eta_date == D(2026, 1, 27)
    assert rolled.settlement.mode is SettlementMode.CALENDAR_THEN_ROLL
