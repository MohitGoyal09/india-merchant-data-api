"""Calendar toolset: RBI offices, bank holidays, working days."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from imda.api.deps import RequestContext
from imda.api.envelope import Used
from imda.api.serialize import holiday_view, office_view
from imda.domain.calendar import CalendarDataMissing
from imda.mcp.context import Draft, ToolEnv, require_office, resource_text, run_tool
from imda.mcp.params import parse_date
from imda.mcp.schemas import (
    BusinessDayResult,
    HolidaysResult,
    NextBusinessDaysResult,
    OfficesResult,
)
from imda.mcp.tools._common import DATE_NOTE, OFFICE_NOTE, READ_ONLY, UNTRUSTED_NOTE
from imda.models import Dataset, Source

MAX_NEXT_DAYS = 31
_RBI_HOLIDAYS = Used(Source.RBI, Dataset.HOLIDAYS)
_WEEKLY_OFF_NOTE = (
    "Sundays and the 2nd and 4th Saturdays are also non-working days but are not in this list."
)

OFFICES_DESCRIPTION = (
    "List the RBI regional offices (slug, city, state). Use it to find the `office` slug that "
    "other calendar and settlement tools need, for example 'mumbai' or 'new-delhi'. RBI "
    "publishes holidays per regional office, not per state. Takes no arguments. It cannot "
    "tell you which office a particular bank branch belongs to. " + UNTRUSTED_NOTE
)
HOLIDAYS_DESCRIPTION = (
    "List RBI bank holidays for one office in one year, or one month of it. Use it to answer "
    "'what are the bank holidays in March in Mumbai?'. `kind` is 'ni_act' (banks closed) or "
    "'closing_of_accounts' (banks' annual or half-yearly closing). Sundays and the 2nd and 4th "
    "Saturdays are also non-working days but are NOT listed; use check_business_day for a "
    "single date. Only years whose data is loaded are available: otherwise you get "
    "CALENDAR_DATA_MISSING and must tell the user, not assume there are no holidays. Offered "
    "for one office at a time only. " + OFFICE_NOTE + " " + UNTRUSTED_NOTE
)
BUSINESS_DAY_DESCRIPTION = (
    "Say whether one date is a bank working day for an office, and why not if it is not "
    "(Sunday, 2nd or 4th Saturday, or a named holiday). Use it for 'is 2 October a bank "
    "holiday in Chennai?'. It uses RBI's holiday list for that office and year; if that year "
    "is not loaded you get CALENDAR_DATA_MISSING. It does not know about local strikes or "
    "bank-specific closures. " + DATE_NOTE + " " + OFFICE_NOTE + " " + UNTRUSTED_NOTE
)
NEXT_DAYS_DESCRIPTION = (
    "List the next N bank working days after a date (the date itself is excluded) for an "
    "office. Use it for 'what are the next 5 working days after 27 March in Mumbai?'. "
    "`count` is 1 to 31 and defaults to 5. For a settlement date use estimate_settlement_date "
    "instead. " + DATE_NOTE + " " + OFFICE_NOTE
)


def _offices(rc: RequestContext) -> Draft:
    offices = [office_view(o) for o in rc.store.offices()]
    slugs = ", ".join(str(o["slug"]) for o in offices)
    return Draft(
        data={"count": len(offices), "offices": offices},
        summary=f"{len(offices)} RBI regional offices. Office slugs: {slugs}.",
        used=[Used(Source.RBI, Dataset.OFFICES)],
    )


def _holidays(rc: RequestContext, office: str, year: int, month: int | None) -> Draft:
    slug = require_office(rc, office)
    if not rc.has_year(slug, year):
        raise CalendarDataMissing(slug, year)
    found = [h for h in rc.store.holidays(slug, year) if month is None or h.date.month == month]
    holidays = [{k: v for k, v in holiday_view(h).items() if k != "office"} for h in found]
    period = f"{year}" if month is None else f"{year}-{month:02d}"
    return Draft(
        data={
            "office": slug,
            "year": year,
            "month": month,
            "count": len(holidays),
            "holidays": holidays,
        },
        summary=f"{len(holidays)} RBI bank holidays for {slug} in {period}. {_WEEKLY_OFF_NOTE}",
        used=[_RBI_HOLIDAYS],
    )


def _business_day(rc: RequestContext, day: dt.date, office: str) -> Draft:
    slug = require_office(rc, office)
    reason = rc.calendar.non_working_reason(slug, day)
    weekday = day.strftime("%A")
    if reason is None:
        summary = f"{day} ({weekday}) is a bank working day for {slug}."
    else:
        summary = f"{day} ({weekday}) is NOT a bank working day for {slug}: {reason}."
    return Draft(
        data={
            "date": day,
            "office": slug,
            "weekday": weekday,
            "is_business_day": reason is None,
            "reason": reason,
        },
        summary=summary,
        used=[_RBI_HOLIDAYS],
    )


def _next_days(rc: RequestContext, day: dt.date, office: str, count: int) -> Draft:
    slug = require_office(rc, office)
    found: list[dt.date] = []
    current = day
    for _ in range(count):
        current = rc.calendar.next_business_day(slug, current)
        found.append(current)
    return Draft(
        data={"office": slug, "after": day, "count": len(found), "dates": found},
        summary=f"Next {len(found)} bank working days after {day} for {slug}: "
        f"{', '.join(d.isoformat() for d in found)}.",
        used=[_RBI_HOLIDAYS],
    )


def register(server: MCPServer, env: ToolEnv) -> None:
    @server.tool(
        name="fetch_all_offices",
        title="List RBI regional offices",
        description=OFFICES_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_all_offices() -> Annotated[CallToolResult, OfficesResult]:
        return run_tool(env, OfficesResult, _offices)

    @server.tool(
        name="fetch_holidays",
        title="RBI bank holidays for an office",
        description=HOLIDAYS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_holidays(
        office: Annotated[str, Field(description="Office slug, e.g. 'mumbai'.")],
        year: Annotated[int, Field(ge=2001, le=2100, description="Calendar year, e.g. 2026.")],
        month: Annotated[
            int | None,
            Field(ge=1, le=12, description="Month 1-12 to narrow to; omit for the year."),
        ] = None,
    ) -> Annotated[CallToolResult, HolidaysResult]:
        return run_tool(env, HolidaysResult, lambda rc: _holidays(rc, office, year, month))

    @server.tool(
        name="check_business_day",
        title="Is this date a bank working day?",
        description=BUSINESS_DAY_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def check_business_day(
        date: Annotated[str, Field(description="Date to check, e.g. '2026-10-02'.")],
        office: Annotated[str, Field(description="Office slug, e.g. 'mumbai'.")],
    ) -> Annotated[CallToolResult, BusinessDayResult]:
        return run_tool(
            env,
            BusinessDayResult,
            lambda rc: _business_day(rc, parse_date("date", date), office),
        )

    @server.tool(
        name="fetch_next_business_days",
        title="Next bank working days after a date",
        description=NEXT_DAYS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_next_business_days(
        date: Annotated[str, Field(description="Start date (excluded), e.g. '2026-03-27'.")],
        office: Annotated[str, Field(description="Office slug, e.g. 'mumbai'.")],
        count: Annotated[
            int, Field(ge=1, le=MAX_NEXT_DAYS, description="How many working days, 1-31.")
        ] = 5,
    ) -> Annotated[CallToolResult, NextBusinessDaysResult]:
        return run_tool(
            env,
            NextBusinessDaysResult,
            lambda rc: _next_days(rc, parse_date("date", date), office, count),
        )

    @server.resource(
        "imda://offices",
        name="offices",
        title="RBI regional offices",
        description="The RBI regional offices and their slugs, as JSON.",
        mime_type="application/json",
    )
    def offices_resource() -> str:
        return resource_text(run_tool(env, OfficesResult, _offices))
