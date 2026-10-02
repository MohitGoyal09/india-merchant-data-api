"""FX toolset: reference rates, conversion, statistics and RBI-versus-FBIL comparison."""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from imda.api.deps import RequestContext
from imda.api.envelope import Used
from imda.api.routes.fx import encode_cursor, fx_used
from imda.api.serialize import (
    as_of_view,
    compare_view,
    conversion_view,
    fx_row,
    stats_view,
    to_jsonable,
)
from imda.domain.fx_service import AsOfResult, Period, SourceChoice, check_range
from imda.mcp.context import Draft, ToolEnv, run_tool
from imda.mcp.errors import RANGE_TOO_LARGE, McpToolError, invalid_request
from imda.mcp.params import (
    AMOUNT_DESCRIPTION,
    AmountInput,
    AnyCurrency,
    ForeignCurrency,
    Source,
    parse_amount,
    parse_convert_currency,
    parse_currency,
    parse_cursor,
    parse_date,
    parse_range,
)
from imda.mcp.schemas import (
    CompareResult,
    ConvertResult,
    FxRateResult,
    FxRatesResult,
    FxStatsResult,
)
from imda.mcp.tools._common import DATE_NOTE, READ_ONLY, UNTRUSTED_NOTE
from imda.models import FxRate
from imda.models import Source as DataSource

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_ROWS_BYTES = 36_000
"""JSON bytes of rows in one fetch_all_fx_rates page; the whole text limit is ~48 KB."""
MAX_STATS_PERIODS = 120
MAX_COMPARE_ROWS = 100
_RATE_NOTE = (
    "Rates are the RBI or FBIL daily reference rates, INR for `unit` units of the currency "
    "(unit is 1, or 100 for JPY and IDR; `rate_per_unit` is INR for exactly one unit). They "
    "are reference rates, not the rate a bank, card network or payment gateway applied."
)
_NO_FUTURE_NOTE = (
    "A date with no publication (weekend, holiday, or not yet published) falls back to the "
    "latest earlier rate and says why."
)

RATE_DESCRIPTION = (
    "Get the RBI/FBIL reference rate for one currency that was in force on a date. Use it for "
    "'what was the USD rate on 14 September 2026?'. `currency` is USD, GBP, EUR, JPY, AED or "
    "IDR. `source` is 'auto' (default: FBIL from 2018-07-10, RBI before), 'rbi' or 'fbil'. "
    + _NO_FUTURE_NOTE
    + " Check `effective_date` and `reason` and tell the user when they differ from the "
    "requested date. If nothing was published within the lookback window you get "
    "RATE_NOT_FOUND. " + _RATE_NOTE + " " + DATE_NOTE + " " + UNTRUSTED_NOTE
)
RATES_DESCRIPTION = (
    "List daily reference rates for one currency over a date range, oldest first, one row per "
    "published date. Use it for a rate history or to look for a rate on a given day. At most "
    "`limit` rows (1-1000, default 100) per call; if `next_cursor` is not null, call again "
    "with the same arguments plus `cursor` to continue. A page can be shorter than `limit` "
    "when the text size cap is hit; `warnings` says so and next_cursor still continues "
    "correctly. For one date use fetch_fx_rate; for averages use fetch_fx_stats. `source` as "
    "in fetch_fx_rate. Ranges are limited to about 10 years. " + _RATE_NOTE + " " + DATE_NOTE
)
CONVERT_DESCRIPTION = (
    "Convert an amount between INR and a foreign currency (or between two foreign currencies, "
    "crossed through INR) at the reference rate in force on a date. Use it for 'what is USD "
    "1,200 in INR on 24 December 2025?'. `amount` is a positive decimal string with at most 2 "
    "decimals, e.g. '1200.00'. `from_currency` and `to_currency` are INR or USD, GBP, EUR, "
    "JPY, AED, IDR, and must differ. `result` is rounded to 0.01 (half up); `exact` is "
    "unrounded. Check `rates_used` for the dates behind the rate. For an invoice plus a "
    "settlement date use quote_invoice. " + _NO_FUTURE_NOTE + " " + _RATE_NOTE + " " + DATE_NOTE
)
STATS_DESCRIPTION = (
    "Weekly or monthly statistics for one currency's reference rate over a range: average, "
    "min, max, first, last, percent change and volatility (stdev of daily log returns, "
    "percent), on per-unit rates. Use it for 'what was the average USD rate in September "
    "2026?'. `period` is 'month' (default) or 'week' (ISO weeks, Monday to Sunday). At most "
    f"{MAX_STATS_PERIODS} periods per call; a longer range gives RANGE_TOO_LARGE. It does "
    "not forecast. " + DATE_NOTE
)
COMPARE_DESCRIPTION = (
    "Compare RBI's and FBIL's reference rates for one currency on dates where both published "
    "(the overlap is from 2018-07-10 to 2018-07-24 and 2022-04-12 onward; between them only "
    "FBIL exists). Differences are FBIL minus RBI on per-unit rates, in INR and basis points; "
    "a day is flagged above 1 bp. Use it to say whether the two sources agree. With more than "
    f"{MAX_COMPARE_ROWS} overlapping days only flagged rows are listed; `summary` always "
    "covers the whole range. " + DATE_NOTE
)


def _rate_summary(result: AsOfResult) -> str:
    rate = result.rate
    text = (
        f"{rate.unit} {result.currency.value} = {rate.rate} INR "
        f"({rate.source.value} reference rate of {result.effective_date}"
    )
    if rate.unit != 1:
        text += f"; {rate.rate / rate.unit:f} INR per unit"
    text += ")."
    if result.effective_date != result.requested_date:
        reason = result.reason or "no publication on that date"
        text += (
            f" No rate was published on {result.requested_date} ({reason}), "
            f"so this is the latest earlier rate, {result.lag_days} days before."
        )
    return text


def _rate(rc: RequestContext, currency: str, date: str, source: SourceChoice) -> Draft:
    code = parse_currency("currency", currency)
    day = parse_date("date", date)
    result = rc.fx.as_of(code, day, source)
    return Draft(
        data=as_of_view(result),
        summary=_rate_summary(result),
        used=fx_used(rc, [code], [result.rate.source]),
    )


def _sources_of(rows: list[FxRate], source: SourceChoice) -> list[DataSource]:
    found = sorted({r.source for r in rows}, key=lambda s: s.value)
    if found:
        return found
    return [DataSource.RBI, DataSource.FBIL] if source == "auto" else [DataSource(source)]


def _fit_rows(rows: list[FxRate]) -> list[FxRate]:
    """The longest prefix of ``rows`` whose JSON fits ``MAX_ROWS_BYTES``."""
    total = 0
    for index, row in enumerate(rows):
        total += len(json.dumps(to_jsonable(fx_row(row)), separators=(",", ":"))) + 1
        if total > MAX_ROWS_BYTES:
            return rows[: max(index, 1)]
    return rows


def _rates(
    rc: RequestContext,
    currency: str,
    from_date: str,
    to_date: str,
    source: SourceChoice,
    limit: int,
    cursor: str | None,
) -> Draft:
    code = parse_currency("currency", currency)
    start, end = parse_range("from_date", from_date, "to_date", to_date)
    check_range(start, end)
    first = start
    max_page = rc.settings.max_page_size
    if limit > max_page:
        raise invalid_request(f"limit: must be at most {max_page}", "Lower `limit`.")
    if cursor is not None:
        start = max(start, parse_cursor(cursor) + dt.timedelta(days=1))
    rows = rc.fx.rates(code, start, end, source) if start <= end else []
    page = _fit_rows(rows[:limit])
    warnings: list[str] = []
    if len(page) < min(limit, len(rows)):
        warnings.append(
            f"page cut to {len(page)} rows to keep the result small; continue with next_cursor"
        )
    next_cursor = encode_cursor(page[-1].date) if len(rows) > len(page) else None
    more = " More rows follow: pass next_cursor as `cursor`." if next_cursor else ""
    summary = f"{len(page)} {code.value} reference rates from {start} to {end}.{more}"
    return Draft(
        data={
            "currency": code,
            "from_date": first,
            "to_date": end,
            "count": len(page),
            "rates": [fx_row(r) for r in page],
            "next_cursor": next_cursor,
        },
        summary=summary,
        used=fx_used(rc, [code], _sources_of(page, source)),
        warnings=warnings,
    )


def _convert(
    rc: RequestContext,
    amount: AmountInput,
    from_currency: str,
    to_currency: str,
    date: str,
    source: SourceChoice,
) -> Draft:
    value = parse_amount("amount", amount)
    src = parse_convert_currency("from_currency", from_currency)
    dst = parse_convert_currency("to_currency", to_currency)
    day = parse_date("date", date)
    conversion = rc.fx.convert(value, src, dst, day, source)
    used: list[Used] = []
    for step in conversion.rates_used:
        used.extend(fx_used(rc, [step.currency], [step.rate.source]))
    view = conversion_view(conversion)
    data = {
        ("from_currency" if k == "from" else "to_currency" if k == "to" else k): v
        for k, v in view.items()
    }
    basis = "; ".join(
        f"{s.currency.value} {s.rate.source.value} rate of {s.effective_date}"
        + (f" ({s.reason})" if s.effective_date != s.requested_date and s.reason else "")
        for s in conversion.rates_used
    )
    return Draft(
        data=data,
        summary=(
            f"{conversion.amount} {conversion.from_currency} = {conversion.result} "
            f"{conversion.to_currency} (exact {conversion.exact:f}) using {basis}."
        ),
        used=used,
    )


def _stats(
    rc: RequestContext,
    currency: str,
    from_date: str,
    to_date: str,
    period: Period,
) -> Draft:
    code = parse_currency("currency", currency)
    start, end = parse_range("from_date", from_date, "to_date", to_date)
    check_range(start, end)
    rates = rc.fx.rates(code, start, end, "auto")
    stats = rc.fx.stats_of(rates, period)
    if len(stats) > MAX_STATS_PERIODS:
        raise McpToolError(
            RANGE_TOO_LARGE,
            f"The range gives {len(stats)} {period} periods; the limit is {MAX_STATS_PERIODS}",
            "Use a shorter range, or period='month' for a long range.",
        )
    warnings = [] if stats else ["no rates in the requested range"]
    if stats:
        last = stats[-1]
        summary = (
            f"{len(stats)} {period} periods for {code.value} from {start} to {end} "
            f"(INR per unit). Latest {last.period_start} to {last.period_end}: mean "
            f"{last.mean:f}, min {last.min:f}, max {last.max:f}, change {last.change_pct:f}%."
        )
    else:
        summary = f"No {code.value} rates between {start} and {end}."
    return Draft(
        data={
            "currency": code,
            "period": period,
            "from_date": start,
            "to_date": end,
            "count": len(stats),
            "stats": [stats_view(s) for s in stats],
        },
        summary=summary,
        used=fx_used(rc, [code], _sources_of(rates, "auto")),
        warnings=warnings,
    )


def _compare(rc: RequestContext, currency: str, from_date: str, to_date: str) -> Draft:
    code = parse_currency("currency", currency)
    start, end = parse_range("from_date", from_date, "to_date", to_date)
    check_range(start, end)
    report = rc.fx.compare(code, start, end)
    view = compare_view(report)
    rows = list(report.rows)
    scope: Literal["all", "flagged_only"] = "all"
    warnings: list[str] = []
    if len(rows) > MAX_COMPARE_ROWS:
        scope = "flagged_only"
        flagged = [r for r in rows if r.flagged]
        rows = flagged[:MAX_COMPARE_ROWS]
        warnings.append(
            f"{report.summary.overlap_days} overlapping days: only flagged days are listed "
            f"(at most {MAX_COMPARE_ROWS})"
        )
    s = report.summary
    summary = (
        f"{code.value}: {s.overlap_days} day(s) where RBI and FBIL both published, "
        f"{s.flagged_days} differ by more than 1 bp, largest difference "
        f"{s.max_abs_diff_bps:f} bp."
    )
    return Draft(
        data={
            "currency": code,
            "from_date": start,
            "to_date": end,
            "rows_scope": scope,
            "rows": [
                {k: getattr(r, k) for k in ("date", "rbi", "fbil", "diff", "diff_bps", "flagged")}
                for r in rows
            ],
            "summary": view["summary"],
        },
        summary=summary,
        used=fx_used(rc, [code], [DataSource.RBI, DataSource.FBIL]),
        warnings=warnings,
    )


def register(server: MCPServer, env: ToolEnv) -> None:
    @server.tool(
        name="fetch_fx_rate",
        title="FX reference rate on a date",
        description=RATE_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_fx_rate(
        currency: ForeignCurrency,
        date: Annotated[str, Field(description="Date, e.g. '2026-09-14'.")],
        source: Source = "auto",
    ) -> Annotated[CallToolResult, FxRateResult]:
        return run_tool(env, FxRateResult, lambda rc: _rate(rc, currency, date, source))

    @server.tool(
        name="fetch_all_fx_rates",
        title="FX reference rate history",
        description=RATES_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_all_fx_rates(
        currency: ForeignCurrency,
        from_date: Annotated[str, Field(description="First date, e.g. '2026-09-01'.")],
        to_date: Annotated[str, Field(description="Last date, e.g. '2026-09-30'.")],
        source: Source = "auto",
        limit: Annotated[
            int, Field(ge=1, le=MAX_LIMIT, description="Rows per page, 1-1000.")
        ] = DEFAULT_LIMIT,
        cursor: Annotated[
            str | None, Field(description="next_cursor from the previous page, exactly as given.")
        ] = None,
    ) -> Annotated[CallToolResult, FxRatesResult]:
        return run_tool(
            env,
            FxRatesResult,
            lambda rc: _rates(rc, currency, from_date, to_date, source, limit, cursor),
        )

    @server.tool(
        name="convert_currency",
        title="Convert an amount at the reference rate",
        description=CONVERT_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def convert_currency(
        amount: Annotated[
            AmountInput,
            Field(description=AMOUNT_DESCRIPTION),
        ],
        from_currency: AnyCurrency,
        to_currency: AnyCurrency,
        date: Annotated[str, Field(description="Rate date, e.g. '2025-12-24'.")],
        source: Source = "auto",
    ) -> Annotated[CallToolResult, ConvertResult]:
        return run_tool(
            env,
            ConvertResult,
            lambda rc: _convert(rc, amount, from_currency, to_currency, date, source),
        )

    @server.tool(
        name="fetch_fx_stats",
        title="FX statistics per week or month",
        description=STATS_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_fx_stats(
        currency: ForeignCurrency,
        from_date: Annotated[str, Field(description="First date, e.g. '2026-09-01'.")],
        to_date: Annotated[str, Field(description="Last date, e.g. '2026-09-30'.")],
        period: Annotated[
            Literal["week", "month"], Field(description="'month' (default) or 'week'.")
        ] = "month",
    ) -> Annotated[CallToolResult, FxStatsResult]:
        return run_tool(
            env, FxStatsResult, lambda rc: _stats(rc, currency, from_date, to_date, period)
        )

    @server.tool(
        name="compare_fx_sources",
        title="Compare RBI and FBIL rates",
        description=COMPARE_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def compare_fx_sources(
        currency: ForeignCurrency,
        from_date: Annotated[str, Field(description="First date, e.g. '2018-07-10'.")],
        to_date: Annotated[str, Field(description="Last date, e.g. '2018-07-24'.")],
    ) -> Annotated[CallToolResult, CompareResult]:
        return run_tool(env, CompareResult, lambda rc: _compare(rc, currency, from_date, to_date))
