"""``GET /v1/holidays``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from imda.api.deps import Ctx, OfficeSlug
from imda.api.envelope import Used, envelope_example, success
from imda.api.serialize import holiday_view
from imda.domain.calendar import CalendarDataMissing
from imda.models import Dataset, Source

router = APIRouter(prefix="/v1", tags=["holidays"])

_EXAMPLE = [
    {
        "office": "mumbai",
        "date": "2026-01-26",
        "weekday": "Monday",
        "name": "Republic Day",
        "kind": "ni_act",
    }
]
_RBI_HOLIDAYS = Used(Source.RBI, Dataset.HOLIDAYS)


@router.get(
    "/holidays",
    summary="RBI bank holidays",
    description=(
        "Holidays for one office, or every office when `office` is omitted. `kind` is `ni_act` "
        "(Negotiable Instruments Act holiday) or `closing_of_accounts`. A year whose data is "
        "not loaded for the requested office is a 409, never an empty list."
    ),
    responses={
        200: envelope_example(
            _EXAMPLE,
            provenance=[
                {
                    "source": "rbi",
                    "dataset": "holidays",
                    "source_url": "https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx",
                    "fetched_at": "2026-10-02T05:13:18.119836+00:00",
                    "stale": False,
                }
            ],
        )
    },
)
def list_holidays(
    ctx: Ctx,
    year: Annotated[int, Query(ge=2001, le=2100, description="Calendar year.")],
    office: Annotated[OfficeSlug | None, Query(description="Office slug; omit for all.")] = None,
    month: Annotated[int | None, Query(ge=1, le=12, description="Month filter.")] = None,
) -> JSONResponse:
    warnings: list[str] = []
    if office is not None:
        ctx.require_office(office)
        if not ctx.has_year(office, year):
            raise CalendarDataMissing(office, year)
    holidays = ctx.store.holidays(office, year)
    if month is not None:
        holidays = [h for h in holidays if h.date.month == month]
    if office is None:
        missing = [s for s in sorted(ctx.office_slugs) if not ctx.has_year(s, year)]
        if missing:
            warnings.append(
                f"holiday data for {year} is not loaded for {len(missing)} of "
                f"{len(ctx.office_slugs)} offices"
            )
    return success(
        ctx,
        [holiday_view(h) for h in holidays],
        used=[_RBI_HOLIDAYS],
        warnings=warnings,
    )
