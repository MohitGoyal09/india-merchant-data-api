"""Case definitions for ``scripts/run_cases.py``: data first, two small hooks for multi-call cases.

A ``Check`` is ``(path, op, value)``. Paths: ``@status``; ``@header.<name>``; ``@text``; ``@lines``
(the body split into lines); otherwise a JSON path such as ``data.0.slug`` where a segment of the
form ``{key=value}`` picks the first list item whose ``key`` equals ``value``.

Scope: ``both`` runs offline (fixture DB) and live (real DB). ``offline`` asserts exact fixture
values. ``live`` asserts a fact only the real, backfilled DB can show. Inside a ``both`` case,
``offline_checks`` hold the exact values that only the fixtures guarantee.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

Mode = Literal["offline", "live"]
Scope = Literal["both", "offline", "live"]


class Send(Protocol):
    def __call__(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response: ...


Hook = Callable[[Send, httpx.Response], list[str]]


@dataclass(frozen=True)
class Check:
    path: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    path: str
    expect: str
    checks: tuple[Check, ...] = ()
    offline_checks: tuple[Check, ...] = ()
    params: Mapping[str, str] | None = None
    json: Mapping[str, Any] | None = None
    headers: Mapping[str, str] | None = None
    scope: Scope = "both"
    hook: Hook | None = None

    def runs_in(self, mode: Mode) -> bool:
        return self.scope in ("both", mode)


# --------------------------------------------------------------------------- hooks
def roll_lands_on_a_business_day(send: Send, response: httpx.Response) -> list[str]:
    """The ``calendar_then_roll`` ETA must itself be a working day."""
    eta = response.json()["data"]["eta_date"]
    check = send("GET", "/v1/calendar/business-day", params={"date": eta, "office": "mumbai"})
    if check.status_code != 200 or check.json()["data"]["is_business_day"] is not True:
        return [f"calendar_then_roll ETA {eta} is not a business day"]
    return []


def pagination_walks_without_overlap(send: Send, response: httpx.Response) -> list[str]:
    """Follow ``next_cursor`` to the end: no overlap, no gap, same rows as an unpaged call."""
    base = dict(PAGED_PARAMS)
    pages = [response.json()]
    while pages[-1]["meta"]["next_cursor"] and len(pages) < MAX_PAGES:
        page = send(
            "GET", "/v1/fx/rates", params={**base, "cursor": pages[-1]["meta"]["next_cursor"]}
        )
        pages.append(page.json())
    dates = [row["date"] for page in pages for row in page["data"]]
    full = send("GET", "/v1/fx/rates", params={**base, "limit": "1000"}).json()
    failures: list[str] = []
    if len(pages) < 2:
        failures.append("expected at least 2 pages")
    if len(dates) != len(set(dates)):
        failures.append("a date appears on two pages")
    if dates != [row["date"] for row in full["data"]]:
        failures.append("paged dates differ from the unpaged result")
    if pages[-1]["meta"]["next_cursor"] is not None:
        failures.append("page walk did not finish")
    return failures


MAX_PAGES = 50
PAGED_PARAMS = {
    "currency": "USD",
    "from": "2026-09-01",
    "to": "2026-09-24",
    "source": "fbil",
    "limit": "5",
}
MUMBAI = {"office": "mumbai"}
SETTLE_PARAMS = {"captured_at": "2026-03-27T11:00:00+05:30", "office": "mumbai"}
SKIP_APRIL_1 = "To enable to Banks to close their yearly accounts (closing of accounts)"
GANESH = "Ganesh Chaturthi"


def _err(status: int, code: str) -> tuple[Check, ...]:
    return (
        Check("@status", "eq", status),
        Check("error.code", "eq", code),
        Check("error.message", "type", "str"),
        Check("request_id", "type", "str"),
    )


CASES: tuple[Case, ...] = (
    Case(
        "offices: 34, Mumbai is Maharashtra",
        "GET",
        "/v1/offices",
        "200, 34 offices",
        checks=(
            Check("@status", "eq", 200),
            Check("data", "len_eq", 34),
            Check("data.{slug=mumbai}.state", "eq", "Maharashtra"),
        ),
    ),
    Case(
        "holidays: Mumbai 2026 has Republic Day",
        "GET",
        "/v1/holidays",
        "200, 2026-01-26 ni_act",
        params={"office": "mumbai", "year": "2026"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.{date=2026-01-26}.name", "eq", "Republic Day"),
            Check("data.{date=2026-01-26}.kind", "eq", "ni_act"),
        ),
    ),
    Case(
        "holidays: 1 Apr is closing_of_accounts",
        "GET",
        "/v1/holidays",
        "200, 2026-04-01 closing_of_accounts",
        params={"office": "mumbai", "year": "2026", "month": "4"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.{date=2026-04-01}.kind", "eq", "closing_of_accounts"),
            Check("data.{date=2026-04-03}.name", "eq", "Good Friday"),
        ),
    ),
    Case(
        "business-day: 28 Mar is the 4th Saturday",
        "GET",
        "/v1/calendar/business-day",
        "200, false, 4th Saturday",
        params={"date": "2026-03-28", **MUMBAI},
        checks=(
            Check("@status", "eq", 200),
            Check("data.is_business_day", "eq", False),
            Check("data.reason", "eq", "4th Saturday"),
        ),
    ),
    Case(
        "business-day: 31 Mar is Mahavir Jayanti",
        "GET",
        "/v1/calendar/business-day",
        "200, false, Mahavir",
        params={"date": "2026-03-31", **MUMBAI},
        checks=(
            Check("@status", "eq", 200),
            Check("data.is_business_day", "eq", False),
            Check("data.reason", "contains", "Mahavir"),
        ),
    ),
    Case(
        "next-business-days: 3 after Fri 27 Mar",
        "GET",
        "/v1/calendar/next-business-days",
        "200, 30 Mar, 2 Apr, 4 Apr",
        params={"date": "2026-03-27", "n": "3", **MUMBAI},
        checks=(
            Check("@status", "eq", 200),
            Check("data.dates", "eq", ["2026-03-30", "2026-04-02", "2026-04-04"]),
        ),
    ),
    Case(
        "settlement: T+2 from 27 Mar skips 4 days",
        "GET",
        "/v1/settlement/eta",
        "200, ETA 2026-04-02, 4 skipped",
        params=SETTLE_PARAMS,
        checks=(
            Check("@status", "eq", 200),
            Check("data.eta_date", "eq", "2026-04-02"),
            Check("data.skipped", "len_eq", 4),
            Check("data.skipped.0.reason", "eq", "4th Saturday"),
            Check("data.skipped.1.reason", "eq", "Sunday"),
            Check("data.skipped.3.reason", "eq", SKIP_APRIL_1),
        ),
    ),
    Case(
        "settlement: calendar_then_roll differs",
        "GET",
        "/v1/settlement/eta",
        "200, ETA 2026-03-30 (a business day)",
        params={**SETTLE_PARAMS, "mode": "calendar_then_roll"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.mode", "eq", "calendar_then_roll"),
            Check("data.eta_date", "ne", "2026-04-02"),
            Check("data.eta_date", "eq", "2026-03-30"),
        ),
        hook=roll_lands_on_a_business_day,
    ),
    Case(
        "settlement: 20:00Z is already 28 Mar in IST",
        "GET",
        "/v1/settlement/eta",
        "200, capture_date 2026-03-28",
        params={"captured_at": "2026-03-27T20:00:00Z", **MUMBAI},
        checks=(
            Check("@status", "eq", 200),
            Check("data.capture_date", "eq", "2026-03-28"),
            Check("data.eta_date", "eq", "2026-04-02"),
        ),
    ),
    Case(
        "fx as-of: holiday falls back to prior day",
        "GET",
        "/v1/fx/rates/as-of",
        "200, 2026-09-14 -> 2026-09-11 (Ganesh Chaturthi)",
        params={"currency": "USD", "date": "2026-09-14"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.effective_date", "eq", "2026-09-11"),
            Check("data.lag_days", "eq", 3),
            Check("data.reason", "startswith", GANESH),
        ),
        offline_checks=(Check("data.rate.rate", "eq", "95.7245"),),
    ),
    Case(
        "fx as-of: Republic Day -> 23 Jan",
        "GET",
        "/v1/fx/rates/as-of",
        "200, 2026-01-26 -> 2026-01-23 (Republic Day)",
        params={"currency": "USD", "date": "2026-01-26"},
        scope="live",  # the fixtures hold no Jan 2026 FX; the backfilled DB does
        checks=(
            Check("@status", "eq", 200),
            Check("data.effective_date", "eq", "2026-01-23"),
            Check("data.reason", "eq", "Republic Day"),
        ),
    ),
    Case(
        "fx as-of: Saturday names the weekday",
        "GET",
        "/v1/fx/rates/as-of",
        "200, 2026-09-12 -> 2026-09-11 (Saturday)",
        params={"currency": "USD", "date": "2026-09-12"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.effective_date", "eq", "2026-09-11"),
            Check("data.reason", "contains", "Saturday"),
        ),
    ),
    Case(
        "fx rates: Sept 2026 auto, one row per date",
        "GET",
        "/v1/fx/rates",
        "200, unique dates, string rates, provenance",
        params={"currency": "USD", "from": "2026-09-01", "to": "2026-09-30", "source": "auto"},
        checks=(
            Check("@status", "eq", 200),
            Check("data", "unique", "date"),
            Check("data.0.date", "eq", "2026-09-01"),
            Check("data.0.rate", "type", "str"),
            Check("data.0.rate_per_unit", "type", "str"),
            Check("provenance", "len_ge", 1),
            Check("provenance.0.source_url", "startswith", "https://"),
        ),
        offline_checks=(
            Check("data.0.rate", "eq", "94.8697"),
            Check("meta.count", "eq", 21),
            Check("data.{date=2026-09-25}.source", "eq", "rbi"),
        ),
    ),
    Case(
        "fx rates: cursor pages do not overlap",
        "GET",
        "/v1/fx/rates",
        "200, limit=5 -> next_cursor, full walk",
        params=PAGED_PARAMS,
        checks=(
            Check("@status", "eq", 200),
            Check("meta.count", "eq", 5),
            Check("meta.next_cursor", "type", "str"),
        ),
        hook=pagination_walks_without_overlap,
    ),
    Case(
        "fx rates: CSV export",
        "GET",
        "/v1/fx/rates",
        "200 text/csv, header + 4 rows",
        params={
            "currency": "USD",
            "from": "2026-09-21",
            "to": "2026-09-24",
            "source": "fbil",
            "format": "csv",
        },
        checks=(
            Check("@status", "eq", 200),
            Check("@header.content-type", "startswith", "text/csv"),
            Check("@lines.0", "eq", "date,currency,rate,unit,rate_per_unit,source,published_at"),
            Check("@lines", "len_eq", 5),
        ),
        offline_checks=(Check("@lines.1", "startswith", "2026-09-21,USD,95.7991,1,"),),
    ),
    Case(
        "convert: 1000 JPY uses unit 100",
        "GET",
        "/v1/fx/convert",
        "200, 606.20 INR, unit 100",
        params={"amount": "1000", "from": "JPY", "to": "INR", "date": "2026-09-24"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.is_cross_rate", "eq", False),
            Check("data.rates_used.0.rate.unit", "eq", 100),
            Check("data.result", "type", "str"),
        ),
        offline_checks=(Check("data.result", "eq", "606.20"),),
    ),
    Case(
        "convert: INR to USD",
        "GET",
        "/v1/fx/convert",
        "200, 10.43 USD",
        params={"amount": "1000", "from": "INR", "to": "USD", "date": "2026-09-24"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.is_cross_rate", "eq", False),
            Check("data.to", "eq", "USD"),
            Check("data.result", "type", "str"),
        ),
        offline_checks=(Check("data.result", "eq", "10.43"),),
    ),
    Case(
        "convert: USD to EUR is a cross rate",
        "GET",
        "/v1/fx/convert",
        "200, is_cross_rate, 2 rates used",
        params={"amount": "100", "from": "USD", "to": "EUR", "date": "2026-09-24"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.is_cross_rate", "eq", True),
            Check("data.rates_used", "len_eq", 2),
        ),
        offline_checks=(Check("data.result", "eq", "87.84"),),
    ),
    Case(
        "stats: Sept 2026 monthly",
        "GET",
        "/v1/fx/stats",
        "200, count/mean/min/max/volatility",
        params={"currency": "USD", "from": "2026-09-01", "to": "2026-09-24", "period": "month"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.0.count", "type", "int"),
            Check("data.0.mean", "type", "str"),
            Check("data.0.min", "type", "str"),
            Check("data.0.max", "type", "str"),
            Check("data.0.volatility", "type", "str"),
        ),
        offline_checks=(
            Check("data.0.count", "eq", 17),
            Check("data.0.min", "eq", "94.4467"),
            Check("data.0.max", "eq", "95.9433"),
            Check("data.0.volatility", "eq", "0.242674"),
        ),
    ),
    Case(
        "compare: RBI vs FBIL agree in Sept 2026",
        "GET",
        "/v1/fx/compare",
        "200, max_abs_diff_bps 0",
        params={"currency": "USD", "from": "2026-09-01", "to": "2026-09-24"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.summary.max_abs_diff_bps", "num_eq", 0),
            Check("data.summary.flagged_days", "eq", 0),
            Check("data.summary.overlap_days", "ge", 1),
        ),
        offline_checks=(Check("data.summary.overlap_days", "eq", 17),),
    ),
    Case(
        "invoice: quote USD 1200 with settlement",
        "POST",
        "/v1/invoice/quote",
        "200, INR amount + ETA 2026-09-28 + notes",
        json={
            "amount": "1200",
            "currency": "USD",
            "invoice_date": "2026-09-24",
            "captured_at": "2026-09-24T11:00:00+05:30",
            "office": "mumbai",
        },
        checks=(
            Check("@status", "eq", 200),
            Check("data.conversion.to", "eq", "INR"),
            Check("data.conversion.result", "type", "str"),
            Check("data.settlement.eta_date", "eq", "2026-09-28"),
            Check("data.notes", "len_ge", 1),
        ),
        offline_checks=(Check("data.conversion.result", "eq", "115091.88"),),
    ),
    Case(
        "mibor: a 3D tenor spans the weekend",
        "GET",
        "/v1/rates/mibor",
        "200, 3D tenor, spans_weekend true",
        params={"from": "2026-09-01", "to": "2026-09-30"},
        checks=(
            Check("@status", "eq", 200),
            Check("data.{tenor=3D}.spans_weekend", "eq", True),
            Check("data.{tenor=3D}.rate", "type", "str"),
            Check("data.{tenor=O/N}.spans_weekend", "eq", False),
        ),
        offline_checks=(Check("data.{date=2026-09-04}.tenor", "eq", "3D"),),
    ),
    Case(
        "ICS: subscribable Mumbai 2026 feed",
        "GET",
        "/v1/calendar/mumbai.ics",
        "200 text/calendar, CRLF, Republic Day",
        params={"year": "2026"},
        checks=(
            Check("@status", "eq", 200),
            Check("@header.content-type", "startswith", "text/calendar"),
            Check("@text", "startswith", "BEGIN:VCALENDAR"),
            Check("@text", "contains", "\r\n"),
            Check("@text", "contains", "SUMMARY:Republic Day"),
            Check("@text", "endswith", "END:VCALENDAR\r\n"),
        ),
    ),
    Case(
        "error: invalid date format",
        "GET",
        "/v1/calendar/business-day",
        "422 INVALID_REQUEST",
        params={"date": "2026-3-28", **MUMBAI},
        checks=_err(422, "INVALID_REQUEST"),
    ),
    Case(
        "error: from after to",
        "GET",
        "/v1/fx/rates",
        "422 VALIDATION_ERROR",
        params={"currency": "USD", "from": "2026-09-24", "to": "2026-09-21"},
        checks=_err(422, "VALIDATION_ERROR"),
    ),
    Case(
        "error: unknown office",
        "GET",
        "/v1/holidays",
        "404 OFFICE_NOT_FOUND",
        params={"office": "atlantis", "year": "2026"},
        checks=(*_err(404, "OFFICE_NOT_FOUND"), Check("error.details.office", "eq", "atlantis")),
    ),
    Case(
        "error: holiday year not loaded",
        "GET",
        "/v1/holidays",
        "409 CALENDAR_DATA_MISSING",
        params={"office": "mumbai", "year": "2010"},
        scope="offline",  # a deep live backfill would legitimately load 2010
        checks=(*_err(409, "CALENDAR_DATA_MISSING"), Check("error.details.year", "eq", 2010)),
    ),
    Case(
        "error: year before 2001 is out of range",
        "GET",
        "/v1/holidays",
        "422 INVALID_REQUEST",
        params={"office": "mumbai", "year": "1999"},
        checks=_err(422, "INVALID_REQUEST"),
    ),
    Case(
        "error: amount with 3 decimals",
        "GET",
        "/v1/fx/convert",
        "422 VALIDATION_ERROR",
        params={"amount": "10.005", "from": "USD", "to": "INR", "date": "2026-09-24"},
        checks=_err(422, "VALIDATION_ERROR"),
    ),
    Case(
        "meta: request id echoed, envelope complete",
        "GET",
        "/v1/offices",
        "200, X-Request-ID echoed, meta + provenance",
        headers={"X-Request-ID": "case-26-request-id"},
        checks=(
            Check("@status", "eq", 200),
            Check("@header.x-request-id", "eq", "case-26-request-id"),
            Check("meta.count", "type", "int"),
            Check("meta.degraded", "type", "bool"),
            Check("meta.warnings", "type", "list"),
            Check("provenance", "len_ge", 1),
            Check("provenance.0.fetched_at", "type", "str"),
        ),
    ),
    Case(
        "healthz: liveness",
        "GET",
        "/healthz",
        "200 status ok",
        checks=(
            Check("@status", "eq", 200),
            Check("status", "eq", "ok"),
            Check("version", "type", "str"),
        ),
    ),
)
