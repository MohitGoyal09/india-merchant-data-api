"""Settlement toolset: indicative settlement ETA and cross-border invoice quote."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from imda.api.deps import RequestContext
from imda.api.envelope import Used
from imda.api.routes.fx import fx_used
from imda.api.serialize import conversion_view, settlement_view
from imda.domain.fx_service import SourceChoice
from imda.domain.invoice import quote_invoice
from imda.domain.settlement import SettlementEstimate, SettlementMode, estimate_settlement
from imda.mcp.context import Draft, ToolEnv, require_office, run_tool
from imda.mcp.params import (
    ForeignCurrency,
    parse_amount,
    parse_aware_datetime,
    parse_currency,
    parse_date,
)
from imda.mcp.schemas import QuoteResult, SettlementResult
from imda.mcp.tools._common import OFFICE_NOTE, READ_ONLY, UNTRUSTED_NOTE
from imda.models import Dataset
from imda.models import Source as DataSource

MAX_CYCLE_DAYS = 30
_RBI_HOLIDAYS = Used(DataSource.RBI, Dataset.HOLIDAYS)
CycleDays = Annotated[
    int | None,
    Field(
        ge=0,
        le=MAX_CYCLE_DAYS,
        description="Settlement cycle N in T+N. Omit for the server default (2).",
    ),
]

ESTIMATE_DESCRIPTION = (
    "Estimate when a payment captured at a given time will settle (T+N bank working days), "
    "and list the non-working days skipped with their reasons (Sunday, 2nd/4th Saturday, "
    "named holidays). Use it for 'when will a payment captured on 27 March 11:00 settle in "
    "Mumbai?'. `captured_at` is ISO 8601 WITH a UTC offset, e.g. '2026-03-27T11:00:00+05:30'; "
    "the capture date is taken in IST. `cycle_days` is N (0-30, default 2). `mode` "
    "'working_days' (default) counts N working days after capture; 'calendar_then_roll' adds "
    "N calendar days then rolls forward to a working day. The result is an INDICATIVE "
    "estimate from RBI holidays and Razorpay's published T+N rule, not Razorpay's settlement "
    "engine, and says nothing about cut-off times, payment method rules or holds. "
    + OFFICE_NOTE
    + " "
    + UNTRUSTED_NOTE
)
QUOTE_DESCRIPTION = (
    "Quote a foreign-currency invoice: its INR value at the RBI/FBIL reference rate in force "
    "on `invoice_date`, plus (when `captured_at` is given) the estimated settlement date. Use "
    "it for 'a customer paid USD 1,200 on 24 Dec 2025: how much is that in INR and when does "
    "it settle?'. `amount` is a positive decimal string with at most 2 decimals, e.g. "
    "'1200.00'. `currency` is USD, GBP, EUR, JPY, AED or IDR. `invoice_date` is YYYY-MM-DD; "
    "if no rate was published that day (weekend or holiday) the latest earlier rate is used "
    "and `notes` and `rates_used` say which date and why. `captured_at` is optional ISO 8601 "
    "with offset; omit it for a conversion only. This is the reference rate, not the rate a "
    "bank or card network charged. The settlement part is an estimate. "
    + OFFICE_NOTE
    + " "
    + UNTRUSTED_NOTE
)


def _skipped_text(estimate: SettlementEstimate) -> str:
    if not estimate.skipped:
        return "no non-working days in between"
    items = ", ".join(f"{s.date} {s.reason}" for s in estimate.skipped)
    return f"skipped {len(estimate.skipped)} non-working days: {items}"


def _eta_summary(estimate: SettlementEstimate, office: str) -> str:
    return (
        f"Captured {estimate.capture_date} (IST) for {office}, T+{estimate.cycle_days} "
        f"{estimate.mode.value}: estimated settlement {estimate.eta_date} "
        f"({estimate.eta_date.strftime('%A')}); {_skipped_text(estimate)}. "
        "This is an estimate, not a commitment."
    )


def _estimate(
    rc: RequestContext,
    office: str,
    captured_at: str,
    cycle_days: int | None,
    mode: SettlementMode,
) -> Draft:
    slug = require_office(rc, office)
    moment = parse_aware_datetime("captured_at", captured_at)
    cycle = rc.settings.settlement_cycle_days if cycle_days is None else cycle_days
    estimate = estimate_settlement(moment, slug, cycle, rc.calendar, mode)
    return Draft(
        data=settlement_view(estimate, slug),
        summary=_eta_summary(estimate, slug),
        used=[_RBI_HOLIDAYS],
    )


def _quote(
    rc: RequestContext,
    amount: str,
    currency: str,
    invoice_date: str,
    office: str,
    captured_at: str | None,
    cycle_days: int | None,
) -> Draft:
    slug = require_office(rc, office)
    when = parse_date("invoice_date", invoice_date)
    value = parse_amount("amount", amount)
    code = parse_currency("currency", currency)
    moment = None if captured_at is None else parse_aware_datetime("captured_at", captured_at)
    cycle = rc.settings.settlement_cycle_days if cycle_days is None else cycle_days
    source: SourceChoice = "auto"
    result = quote_invoice(
        amount=value,
        currency=code,
        invoice_date=when,
        office=slug,
        fx=rc.fx,
        calendar=rc.calendar,
        captured_at=moment,
        cycle_days=cycle,
        mode=SettlementMode.WORKING_DAYS,
        source=source,
    )
    used: list[Used] = []
    for step in result.conversion.rates_used:
        used.extend(fx_used(rc, [step.currency], [step.rate.source]))
    if result.settlement is not None:
        used.append(_RBI_HOLIDAYS)
    conversion = conversion_view(result.conversion)
    conversion = {
        ("from_currency" if k == "from" else "to_currency" if k == "to" else k): v
        for k, v in conversion.items()
    }
    rate = result.conversion.rates_used[0]
    summary = (
        f"{result.conversion.amount} {result.conversion.from_currency} = "
        f"{result.conversion.result} INR at the {rate.rate.source.value} reference rate "
        f"of {rate.effective_date}."
    )
    if result.settlement is not None:
        summary += " " + _eta_summary(result.settlement, slug)
    return Draft(
        data={
            "invoice_date": when,
            "office": slug,
            "conversion": conversion,
            "settlement": (
                None if result.settlement is None else settlement_view(result.settlement, slug)
            ),
            "notes": list(result.notes),
        },
        summary=summary,
        used=used,
    )


def register(server: MCPServer, env: ToolEnv) -> None:
    @server.tool(
        name="estimate_settlement_date",
        title="Estimate settlement date",
        description=ESTIMATE_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def estimate_settlement_date(
        captured_at: Annotated[
            str,
            Field(
                description="Capture time, ISO 8601 with offset, e.g. '2026-03-27T11:00:00+05:30'."
            ),
        ],
        office: Annotated[str, Field(description="Office slug, e.g. 'mumbai'.")],
        cycle_days: CycleDays = None,
        mode: Annotated[
            Literal["working_days", "calendar_then_roll"],
            Field(description="'working_days' (default) or 'calendar_then_roll'."),
        ] = "working_days",
    ) -> Annotated[CallToolResult, SettlementResult]:
        return run_tool(
            env,
            SettlementResult,
            lambda rc: _estimate(rc, office, captured_at, cycle_days, SettlementMode(mode)),
        )

    @server.tool(
        name="quote_invoice",
        title="Quote a foreign-currency invoice",
        description=QUOTE_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def quote_invoice_tool(
        amount: Annotated[str, Field(description="Positive decimal string, e.g. '1200.00'.")],
        currency: ForeignCurrency,
        invoice_date: Annotated[str, Field(description="Invoice date, e.g. '2025-12-24'.")],
        office: Annotated[str, Field(description="Office slug, e.g. 'mumbai'.")],
        captured_at: Annotated[
            str | None,
            Field(description="Optional capture time, ISO 8601 with offset, for a settlement ETA."),
        ] = None,
        cycle_days: CycleDays = None,
    ) -> Annotated[CallToolResult, QuoteResult]:
        return run_tool(
            env,
            QuoteResult,
            lambda rc: _quote(rc, amount, currency, invoice_date, office, captured_at, cycle_days),
        )
