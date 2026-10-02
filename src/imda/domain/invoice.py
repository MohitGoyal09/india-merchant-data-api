"""Cross-border invoice quote: INR value plus an indicative settlement date. Pure logic."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from imda.domain.calendar import HolidayCalendar
from imda.domain.fx_service import Conversion, FxService, SourceChoice
from imda.domain.settlement import SettlementEstimate, SettlementMode, estimate_settlement
from imda.models import Currency

SETTLEMENT_NOTE = "settlement estimate is indicative, not Razorpay's settlement engine"


@dataclass(frozen=True, slots=True)
class InvoiceQuote:
    conversion: Conversion
    settlement: SettlementEstimate | None
    notes: tuple[str, ...]


def quote_invoice(
    *,
    amount: Decimal,
    currency: Currency,
    invoice_date: dt.date,
    office: str,
    fx: FxService,
    calendar: HolidayCalendar,
    captured_at: dt.datetime | None = None,
    cycle_days: int,
    mode: SettlementMode = SettlementMode.WORKING_DAYS,
    source: SourceChoice = "auto",
) -> InvoiceQuote:
    """Convert ``amount`` to INR at the as-of rate for ``invoice_date``.

    A settlement estimate is added only when ``captured_at`` is given.
    """
    conversion = fx.convert(amount, currency.value, "INR", invoice_date, source)
    notes: list[str] = []
    rate = conversion.rates_used[0]
    if rate.effective_date != invoice_date:
        reason = rate.reason or "no publication on this date"
        notes.append(f"rate is from {rate.effective_date} ({reason})")
    settlement: SettlementEstimate | None = None
    if captured_at is not None:
        settlement = estimate_settlement(captured_at, office, cycle_days, calendar, mode)
        notes.append(SETTLEMENT_NOTE)
    return InvoiceQuote(conversion=conversion, settlement=settlement, notes=tuple(notes))
