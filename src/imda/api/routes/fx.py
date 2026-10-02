"""FX reference rates: history (JSON or CSV), as-of, convert, stats, compare."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
from collections.abc import Iterable
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from imda.api.deps import (
    MAX_API_DATE,
    MIN_API_DATE,
    AmountText,
    ConvertCurrency,
    Ctx,
    CurrencyCode,
    IsoDate,
    RequestContext,
)
from imda.api.envelope import Used, assess, envelope_example, success
from imda.api.errors import invalid_request
from imda.api.serialize import (
    FX_COLUMNS,
    as_of_view,
    compare_view,
    conversion_view,
    fx_row,
    stats_view,
    to_jsonable,
)
from imda.domain.fx_service import Period, SourceChoice, check_range
from imda.errors import InvalidInput
from imda.models import Currency, Dataset, FxRate, Source

router = APIRouter(prefix="/v1/fx", tags=["fx"])

DEFAULT_PAGE_SIZE = 1000
_FX = Dataset.FX
_ROW_EXAMPLE = {
    "date": "2026-09-30",
    "currency": "USD",
    "rate": "88.3125",
    "unit": 1,
    "rate_per_unit": "88.3125",
    "source": "fbil",
    "published_at": "2026-09-30T12:30:00+05:30",
}
_CURRENCY_Q = Query(description="USD, GBP, EUR, JPY, AED or IDR")
_SOURCE_Q = Query(description="auto: FBIL from 2018-07-10, RBI before; or force rbi / fbil")


def encode_cursor(day: dt.date) -> str:
    return base64.urlsafe_b64encode(day.isoformat().encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dt.date:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        day = dt.date.fromisoformat(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, OverflowError, UnicodeError):
        raise InvalidInput("invalid cursor") from None
    if not MIN_API_DATE <= day <= MAX_API_DATE:
        raise InvalidInput("invalid cursor")
    return day


def _cursor_start(cursor: str) -> dt.date:
    try:
        return decode_cursor(cursor) + dt.timedelta(days=1)
    except InvalidInput as exc:
        raise invalid_request("query", "cursor", str(exc)) from None


def fx_used(
    ctx: RequestContext, currencies: Iterable[Currency], sources: Iterable[Source]
) -> list[Used]:
    """One ``Used`` per source, carrying the newest date held for any of the currencies."""
    wanted = list(currencies)
    used: list[Used] = []
    for source in dict.fromkeys(sources):
        dates = [ctx.store.latest_fx_date(c, source) for c in wanted]
        known = [d for d in dates if d is not None]
        used.append(Used(source, _FX, max(known) if known else None))
    return used


def _sources_of(rows: Iterable[FxRate], explicit: SourceChoice) -> list[Source]:
    """The sources that supplied ``rows``; with none, the ones the query would have drawn on."""
    found = sorted({r.source for r in rows}, key=lambda s: s.value)
    if found:
        return found
    return [Source.RBI, Source.FBIL] if explicit == "auto" else [Source(explicit)]


def _wants_csv(fmt: str | None, accept: str | None) -> bool:
    if fmt is not None:
        return fmt == "csv"
    return "text/csv" in (accept or "").lower()


def _csv_response(ctx: RequestContext, rows: list[FxRate], used: list[Used], name: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(FX_COLUMNS)
    for row in rows:
        view = to_jsonable(fx_row(row))
        writer.writerow(["" if view[c] is None else view[c] for c in FX_COLUMNS])
    assessment = assess(ctx, used)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "X-IMDA-Sources": ",".join(p.source.value for p in assessment.provenance),
            "X-IMDA-Degraded": str(assessment.degraded).lower(),
            "X-IMDA-Stale": str(assessment.any_stale).lower(),
        },
    )


@router.get(
    "/rates",
    summary="FX reference rate history",
    description=(
        "Oldest first, one row per date. JSON is paginated with an opaque `cursor`; CSV "
        "(`format=csv` or `Accept: text/csv`) returns the whole range, capped at 10 years. "
        "`rate` is INR for `unit` units of the currency; `rate_per_unit` is INR for one."
    ),
    responses={
        200: {
            **envelope_example([_ROW_EXAMPLE]),
            "content": {
                **envelope_example([_ROW_EXAMPLE])["content"],
                "text/csv": {"example": ",".join(FX_COLUMNS) + "\r\n"},
            },
        }
    },
)
def fx_rates(
    request: Request,
    ctx: Ctx,
    currency: Annotated[CurrencyCode, _CURRENCY_Q],
    from_: Annotated[IsoDate, Query(alias="from", description="First date, YYYY-MM-DD")],
    to: Annotated[IsoDate, Query(description="Last date, YYYY-MM-DD")],
    source: Annotated[SourceChoice, _SOURCE_Q] = "auto",
    limit: Annotated[int | None, Query(ge=1, description="Page size (JSON only)")] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor from meta.next_cursor")] = None,
    format: Annotated[Literal["json", "csv"] | None, Query()] = None,
) -> Response:
    check_range(from_, to)
    if _wants_csv(format, request.headers.get("accept")):
        rows = ctx.fx.rates(currency, from_, to, source)
        name = f"fx_{currency.value}_{from_}_{to}.csv"
        return _csv_response(ctx, rows, fx_used(ctx, [currency], _sources_of(rows, source)), name)
    max_page = ctx.settings.max_page_size
    page_size = min(DEFAULT_PAGE_SIZE, max_page) if limit is None else limit
    if page_size > max_page:
        raise invalid_request("query", "limit", f"limit must be at most {max_page}")
    start = from_
    if cursor is not None:
        start = max(from_, _cursor_start(cursor))
    rows = ctx.fx.rates(currency, start, to, source) if start <= to else []
    page = rows[:page_size]
    next_cursor = encode_cursor(page[-1].date) if len(rows) > page_size else None
    return success(
        ctx,
        [fx_row(r) for r in page],
        used=fx_used(ctx, [currency], _sources_of(page, source)),
        next_cursor=next_cursor,
    )


@router.get(
    "/rates/as-of",
    summary="The rate in force on a date",
    description=(
        "Returns the rate published on the date, or the latest earlier one with `reason` "
        "(weekend, holiday name, not yet published, source gap)."
    ),
    responses={
        200: envelope_example(
            {
                "currency": "USD",
                "requested_date": "2026-01-26",
                "effective_date": "2026-01-23",
                "lag_days": 3,
                "reason": "Republic Day",
                "rate": _ROW_EXAMPLE,
            }
        )
    },
)
def fx_as_of(
    ctx: Ctx,
    currency: Annotated[CurrencyCode, _CURRENCY_Q],
    date: Annotated[IsoDate, Query(description="YYYY-MM-DD")],
    source: Annotated[SourceChoice, _SOURCE_Q] = "auto",
) -> JSONResponse:
    result = ctx.fx.as_of(currency, date, source)
    used = fx_used(ctx, [currency], [result.rate.source])
    return success(ctx, as_of_view(result), used=used, count=1)


@router.get(
    "/convert",
    summary="Convert between INR and a foreign currency",
    description=(
        "One side must be INR (or both foreign: crossed through INR). `result` is rounded to "
        "0.01 half-up; `exact` is unrounded. The rate is the as-of rate for `date`."
    ),
    responses={
        200: envelope_example(
            {
                "amount": "100.00",
                "from": "USD",
                "to": "INR",
                "result": "8831.25",
                "exact": "8831.2500",
                "is_cross_rate": False,
                "rates_used": [
                    {
                        "currency": "USD",
                        "requested_date": "2026-09-30",
                        "effective_date": "2026-09-30",
                        "lag_days": 0,
                        "reason": None,
                        "rate": _ROW_EXAMPLE,
                    }
                ],
            }
        )
    },
)
def fx_convert(
    ctx: Ctx,
    amount: Annotated[AmountText, Query(description="Positive, at most 2 decimal places")],
    from_: Annotated[ConvertCurrency, Query(alias="from", min_length=3, max_length=3)],
    to: Annotated[ConvertCurrency, Query(min_length=3, max_length=3)],
    date: Annotated[IsoDate, Query(description="YYYY-MM-DD")],
    source: Annotated[SourceChoice, _SOURCE_Q] = "auto",
) -> JSONResponse:
    conversion = ctx.fx.convert(amount, from_, to, date, source)
    used: list[Used] = []
    for step in conversion.rates_used:
        used.extend(fx_used(ctx, [step.currency], [step.rate.source]))
    return success(ctx, conversion_view(conversion), used=used, count=1)


@router.get(
    "/stats",
    summary="Per-week or per-month statistics",
    description=(
        "Mean, min, max, first, last, change and volatility (sample stdev of daily log "
        "returns, percent) on per-unit rates."
    ),
    responses={
        200: envelope_example(
            [
                {
                    "period_start": "2026-09-01",
                    "period_end": "2026-09-30",
                    "count": 21,
                    "mean": "88.1000000000",
                    "min": "87.9",
                    "max": "88.4",
                    "first": "88.0",
                    "last": "88.3125",
                    "change_pct": "0.355114",
                    "volatility": "0.120000",
                }
            ]
        )
    },
)
def fx_stats(
    ctx: Ctx,
    currency: Annotated[CurrencyCode, _CURRENCY_Q],
    from_: Annotated[IsoDate, Query(alias="from")],
    to: Annotated[IsoDate, Query()],
    period: Annotated[Period, Query()] = "month",
    source: Annotated[SourceChoice, _SOURCE_Q] = "auto",
) -> JSONResponse:
    check_range(from_, to)
    rates = ctx.fx.rates(currency, from_, to, source)
    stats = ctx.fx.stats_of(rates, period)
    sources = _sources_of(rates, source)
    warnings = [] if stats else ["no rates in the requested range"]
    return success(
        ctx,
        [stats_view(s) for s in stats],
        used=fx_used(ctx, [currency], sources),
        warnings=warnings,
    )


@router.get(
    "/compare",
    summary="RBI against FBIL on overlapping dates",
    description="Differences are FBIL minus RBI on per-unit rates; flagged above 1 bp.",
    responses={
        200: envelope_example(
            {
                "currency": "USD",
                "from": "2018-07-10",
                "to": "2018-07-24",
                "rows": [
                    {
                        "date": "2018-07-10",
                        "rbi": "68.4",
                        "fbil": "68.4",
                        "diff": "0.0",
                        "diff_bps": "0.0000",
                        "flagged": False,
                    }
                ],
                "summary": {
                    "overlap_days": 11,
                    "flagged_days": 0,
                    "max_abs_diff_bps": "0.0000",
                    "rbi_only_days": 0,
                    "fbil_only_days": 0,
                },
            }
        )
    },
)
def fx_compare(
    ctx: Ctx,
    currency: Annotated[CurrencyCode, _CURRENCY_Q],
    from_: Annotated[IsoDate, Query(alias="from")],
    to: Annotated[IsoDate, Query()],
) -> JSONResponse:
    check_range(from_, to)
    report = ctx.fx.compare(currency, from_, to)
    used = fx_used(ctx, [currency], [Source.RBI, Source.FBIL])
    return success(ctx, compare_view(report), used=used, count=len(report.rows))
