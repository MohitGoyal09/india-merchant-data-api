"""Business-day engine and the subscribable ICS feed."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse

from imda.api.deps import Ctx, IsoDate, OfficeSlug
from imda.api.envelope import Used, envelope_example, success
from imda.api.errors import ERROR_RESPONSES
from imda.api.ics import render_calendar
from imda.domain.calendar import CalendarDataMissing
from imda.models import Dataset, HolidayKind, Source

router = APIRouter(prefix="/v1/calendar", tags=["calendar"])

MAX_NEXT_DAYS = 31
ICS_CACHE_SECONDS = 3600
_RBI_HOLIDAYS = Used(Source.RBI, Dataset.HOLIDAYS)
_HOLIDAYS_PROVENANCE = [
    {
        "source": "rbi",
        "dataset": "holidays",
        "source_url": "https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx",
        "fetched_at": "2026-10-02T05:13:18.119836+00:00",
        "stale": False,
    }
]


@router.get(
    "/business-day",
    summary="Is a date a working day for an office?",
    responses={
        200: envelope_example(
            {
                "date": "2026-03-28",
                "office": "mumbai",
                "weekday": "Saturday",
                "is_business_day": False,
                "reason": "4th Saturday",
            },
            provenance=_HOLIDAYS_PROVENANCE,
        )
    },
)
def business_day(
    ctx: Ctx,
    date: Annotated[IsoDate, Query(description="YYYY-MM-DD")],
    office: Annotated[OfficeSlug, Query(description="Office slug, e.g. mumbai")],
) -> JSONResponse:
    ctx.require_office(office)
    reason = ctx.calendar.non_working_reason(office, date)
    data = {
        "date": date,
        "office": office,
        "weekday": date.strftime("%A"),
        "is_business_day": reason is None,
        "reason": reason,
    }
    return success(ctx, data, used=[_RBI_HOLIDAYS])


@router.get(
    "/next-business-days",
    summary="The next N working days after a date",
    responses={
        200: envelope_example(
            {
                "office": "mumbai",
                "after": "2026-03-27",
                "n": 2,
                "dates": ["2026-03-30", "2026-04-01"],
            },
            provenance=_HOLIDAYS_PROVENANCE,
        )
    },
)
def next_business_days(
    ctx: Ctx,
    date: Annotated[IsoDate, Query(description="Start date (exclusive), YYYY-MM-DD")],
    office: Annotated[OfficeSlug, Query(description="Office slug, e.g. mumbai")],
    n: Annotated[int, Query(ge=1, le=MAX_NEXT_DAYS, description="How many days")] = 1,
) -> JSONResponse:
    ctx.require_office(office)
    found: list[dt.date] = []
    current = date
    for _ in range(n):
        current = ctx.calendar.next_business_day(office, current)
        found.append(current)
    data = {"office": office, "after": date, "n": n, "dates": found}
    return success(ctx, data, used=[_RBI_HOLIDAYS], count=len(found))


@router.get(
    "/{office}.ics",
    summary="Subscribable iCalendar feed of RBI holidays",
    response_class=Response,
    responses={
        200: {
            "description": "RFC 5545 calendar, one all-day event per holiday",
            "content": {"text/calendar": {"example": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n..."}},
        },
        **{code: spec for code, spec in ERROR_RESPONSES.items() if code != 200},
    },
)
def calendar_feed(
    ctx: Ctx,
    office: str,
    year: Annotated[int, Query(ge=2001, le=2100, description="Calendar year")],
    include_weekly_off: Annotated[
        bool, Query(description="Also add the 2nd and 4th Saturdays")
    ] = False,
) -> Response:
    slug = ctx.require_office(office.strip().lower())
    if not ctx.has_year(slug, year):
        raise CalendarDataMissing(slug, year)
    office_row = next(o for o in ctx.store.offices() if o.slug == slug)
    fetch = ctx.store.latest_fetch(Source.RBI, Dataset.HOLIDAYS)
    stamp = fetch.fetched_at if fetch else ctx.now()
    holidays = [
        h
        for h in ctx.store.holidays(slug, year)
        if ctx.settings.closing_of_accounts_is_holiday
        or h.kind is not HolidayKind.CLOSING_OF_ACCOUNTS
    ]
    body = render_calendar(
        office_row,
        year,
        holidays,
        stamp=stamp,
        include_weekly_off=include_weekly_off,
    )
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": f"public, max-age={ICS_CACHE_SECONDS}"},
    )
