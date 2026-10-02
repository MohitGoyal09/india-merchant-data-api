"""``GET /v1/settlement/eta``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from imda.api.deps import AwareDatetime, Ctx, OfficeSlug
from imda.api.envelope import Used, envelope_example, success
from imda.api.serialize import SETTLEMENT_DISCLAIMER, settlement_view
from imda.domain.settlement import SettlementMode, estimate_settlement
from imda.models import Dataset, Source

router = APIRouter(prefix="/v1/settlement", tags=["settlement"])

MAX_CYCLE_DAYS = 30
_EXAMPLE = {
    "office": "mumbai",
    "captured_at": "2026-03-27T11:00:00+05:30",
    "capture_date": "2026-03-27",
    "cycle_days": 2,
    "mode": "working_days",
    "eta_date": "2026-04-01",
    "counted_days": ["2026-03-30", "2026-04-01"],
    "skipped": [
        {"date": "2026-03-28", "reason": "4th Saturday"},
        {"date": "2026-03-29", "reason": "Sunday"},
        {"date": "2026-03-31", "reason": "Banks' closing of accounts"},
    ],
    "disclaimer": SETTLEMENT_DISCLAIMER,
}


@router.get(
    "/eta",
    summary="Indicative settlement date for a captured payment",
    description=(
        "`working_days` counts N business days after the capture date (the docs' wording). "
        "`calendar_then_roll` adds N calendar days and rolls forward to a business day (the "
        "docs' worked example). `captured_at` must carry a UTC offset; send `+` as `%2B`."
    ),
    responses={200: envelope_example(_EXAMPLE)},
)
def settlement_eta(
    ctx: Ctx,
    captured_at: Annotated[AwareDatetime, Query(description="ISO 8601 with offset")],
    office: Annotated[OfficeSlug, Query(description="Office slug, e.g. mumbai")],
    cycle_days: Annotated[int | None, Query(ge=0, le=MAX_CYCLE_DAYS)] = None,
    mode: SettlementMode = SettlementMode.WORKING_DAYS,
) -> JSONResponse:
    ctx.require_office(office)
    cycle = ctx.settings.settlement_cycle_days if cycle_days is None else cycle_days
    estimate = estimate_settlement(captured_at, office, cycle, ctx.calendar, mode)
    return success(
        ctx,
        settlement_view(estimate, office),
        used=[Used(Source.RBI, Dataset.HOLIDAYS)],
        count=1,
    )
