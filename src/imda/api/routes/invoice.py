"""``POST /v1/invoice/quote``: INR value of a foreign-currency invoice plus settlement ETA."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from imda.api.deps import AmountText, AwareDatetime, Ctx, CurrencyCode, IsoDate, OfficeSlug
from imda.api.envelope import Used, envelope_example, success
from imda.api.routes.fx import fx_used
from imda.api.serialize import conversion_view, settlement_view
from imda.domain.fx_service import SourceChoice
from imda.domain.invoice import quote_invoice
from imda.domain.settlement import SettlementMode
from imda.models import Currency, Dataset, Source

router = APIRouter(prefix="/v1/invoice", tags=["invoice"])

MAX_CYCLE_DAYS = 30


class InvoiceQuoteRequest(BaseModel):
    """Body of ``POST /v1/invoice/quote``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "amount": "1250.50",
                "currency": "USD",
                "invoice_date": "2026-09-30",
                "office": "mumbai",
                "captured_at": "2026-09-30T11:00:00+05:30",
            }
        },
    )

    amount: Annotated[
        AmountText, Field(json_schema_extra={"type": "string", "examples": ["1250.50"]})
    ]
    currency: CurrencyCode
    invoice_date: IsoDate
    office: OfficeSlug
    captured_at: AwareDatetime | None = None
    cycle_days: Annotated[int | None, Field(ge=0, le=MAX_CYCLE_DAYS)] = None
    mode: SettlementMode = SettlementMode.WORKING_DAYS
    source: SourceChoice = "auto"


_EXAMPLE: dict[str, object] = {
    "invoice_date": "2026-09-30",
    "office": "mumbai",
    "conversion": {
        "amount": "1250.50",
        "from": "USD",
        "to": "INR",
        "result": "110434.65",
        "exact": "110434.65625",
        "is_cross_rate": False,
        "rates_used": [],
    },
    "settlement": None,
    "notes": [],
}


@router.post(
    "/quote",
    summary="Quote a cross-border invoice",
    description=(
        "Converts `amount` at the as-of rate for `invoice_date`. When `captured_at` is given, "
        "adds an indicative settlement estimate for `office`. `amount` is a string decimal, "
        "positive, at most 2 decimal places."
    ),
    responses={200: envelope_example(_EXAMPLE)},
)
def quote(ctx: Ctx, body: InvoiceQuoteRequest) -> JSONResponse:
    ctx.require_office(body.office)
    cycle = ctx.settings.settlement_cycle_days if body.cycle_days is None else body.cycle_days
    result = quote_invoice(
        amount=body.amount,
        currency=Currency(body.currency),
        invoice_date=body.invoice_date,
        office=body.office,
        fx=ctx.fx,
        calendar=ctx.calendar,
        captured_at=body.captured_at,
        cycle_days=cycle,
        mode=body.mode,
        source=body.source,
    )
    used: list[Used] = []
    for step in result.conversion.rates_used:
        used.extend(fx_used(ctx, [step.currency], [step.rate.source]))
    if result.settlement is not None:
        used.append(Used(Source.RBI, Dataset.HOLIDAYS))
    data = {
        "invoice_date": body.invoice_date,
        "office": body.office,
        "conversion": conversion_view(result.conversion),
        "settlement": (
            None if result.settlement is None else settlement_view(result.settlement, body.office)
        ),
        "notes": list(result.notes),
    }
    return success(ctx, data, used=used, count=1)
