"""Typed tool results. They become each tool's ``outputSchema`` and ``structuredContent``.

Decimals are decimal strings, dates are ISO text, datetimes are ISO 8601 with offset. Every
result also carries ``provenance`` (where the data came from) and ``warnings`` (stale or
degraded data, partial results).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProvenanceItem(_Model):
    source: str = Field(description="Upstream publisher: 'rbi' or 'fbil'.")
    dataset: str
    source_url: str
    fetched_at: str | None = Field(description="When we last fetched it (ISO 8601, UTC).")
    stale: bool = Field(description="True when newer data should exist than what we hold.")


class ToolResult(_Model):
    provenance: list[ProvenanceItem]
    warnings: list[str] = Field(
        description="Stale or degraded data and partial results. Tell the user about these."
    )


# ----------------------------------------------------------------------------- calendar
class OfficeItem(_Model):
    slug: str = Field(description="Use this as the `office` argument of other tools.")
    name: str
    state: str | None
    rbi_id: int


class OfficesResult(ToolResult):
    count: int
    offices: list[OfficeItem]


class HolidayItem(_Model):
    date: str
    weekday: str
    name: str
    kind: Literal["ni_act", "closing_of_accounts"] = Field(
        description="ni_act: banks closed. closing_of_accounts: annual or half-yearly closing."
    )


class HolidaysResult(ToolResult):
    office: str
    year: int
    month: int | None
    count: int
    holidays: list[HolidayItem]


class BusinessDayResult(ToolResult):
    date: str
    office: str
    weekday: str
    is_business_day: bool
    reason: str | None = Field(description="Why it is not a working day; null when it is one.")


class NextBusinessDaysResult(ToolResult):
    office: str
    after: str = Field(description="The start date (exclusive).")
    count: int
    dates: list[str]


# ----------------------------------------------------------------------------- settlement
class SkippedDayItem(_Model):
    date: str
    reason: str


class SettlementBody(_Model):
    office: str
    captured_at: str
    capture_date: str = Field(description="The capture date in IST (T).")
    cycle_days: int
    mode: Literal["working_days", "calendar_then_roll"]
    eta_date: str = Field(description="Estimated settlement date.")
    counted_days: list[str]
    skipped: list[SkippedDayItem] = Field(
        description="Non-working days between capture and the ETA, with reasons."
    )
    disclaimer: str


class SettlementResult(SettlementBody, ToolResult):
    pass


# ----------------------------------------------------------------------------- fx
class FxRateRow(_Model):
    date: str
    currency: str
    rate: str = Field(description="INR for `unit` units of the currency.")
    unit: int = Field(description="1, or 100 for JPY and IDR.")
    rate_per_unit: str = Field(description="INR for exactly one unit.")
    source: str
    published_at: str | None


class AsOfItem(_Model):
    currency: str
    requested_date: str
    effective_date: str = Field(description="The date the rate was actually published.")
    lag_days: int
    reason: str | None = Field(description="Why the rate is from an earlier date, if it is.")
    rate: FxRateRow


class FxRateResult(AsOfItem, ToolResult):
    pass


class FxRatesResult(ToolResult):
    currency: str
    from_date: str
    to_date: str
    count: int
    rates: list[FxRateRow]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null when there is no more."
    )


class ConversionBody(_Model):
    amount: str
    from_currency: str
    to_currency: str
    result: str = Field(description="Rounded to 0.01, half up.")
    exact: str = Field(description="Unrounded.")
    is_cross_rate: bool
    rates_used: list[AsOfItem]


class ConvertResult(ConversionBody, ToolResult):
    pass


class QuoteResult(ToolResult):
    invoice_date: str
    office: str
    conversion: ConversionBody
    settlement: SettlementBody | None = Field(description="Null when `captured_at` was not given.")
    notes: list[str]


class StatsItem(_Model):
    period_start: str
    period_end: str
    count: int
    mean: str
    min: str
    max: str
    first: str
    last: str
    change_pct: str
    volatility: str | None = Field(description="Percent; null with fewer than 3 points.")


class FxStatsResult(ToolResult):
    currency: str
    period: Literal["week", "month"]
    from_date: str
    to_date: str
    count: int
    stats: list[StatsItem]


class CompareRowItem(_Model):
    date: str
    rbi: str
    fbil: str
    diff: str
    diff_bps: str
    flagged: bool


class CompareSummaryItem(_Model):
    overlap_days: int
    flagged_days: int
    max_abs_diff_bps: str
    rbi_only_days: int
    fbil_only_days: int


class CompareResult(ToolResult):
    currency: str
    from_date: str
    to_date: str
    rows_scope: Literal["all", "flagged_only"]
    rows: list[CompareRowItem]
    summary: CompareSummaryItem


# ----------------------------------------------------------------------------- rates
class MiborItem(_Model):
    date: str
    tenor: str = Field(description="'O/N', or '3D' on Fridays (Friday to Monday).")
    rate: str = Field(description="Percent per annum.")
    spans_weekend: bool
    source: str
    published_at: str | None


class MiborResult(ToolResult):
    from_date: str
    to_date: str
    count: int
    rates: list[MiborItem]


# ----------------------------------------------------------------------------- health
class DriftItem(_Model):
    drifted: bool
    added_keys: list[str]
    removed_keys: list[str]
    changed_keys: list[str]
    note: str | None
    summary: str


class FreshnessItem(_Model):
    latest_date: str | None
    expected_date: str
    lag_business_days: int | None
    stale: bool
    calendar_incomplete: bool


class SourceHealthItem(_Model):
    source: str
    dataset: str
    status: Literal["ok", "degraded", "broken", "unknown"]
    checked_at: str | None
    last_success_at: str | None
    last_error_at: str | None
    last_error: str | None
    drift: DriftItem | None
    freshness: FreshnessItem | None


class RunItem(_Model):
    run_id: str
    kind: str
    status: str
    started_at: str | None
    finished_at: str | None


class SourceHealthResult(ToolResult):
    status: Literal["ok", "degraded", "broken", "unknown"] = Field(
        description="The worst status across all sources."
    )
    sources: list[SourceHealthItem]
    last_runs: list[RunItem]
