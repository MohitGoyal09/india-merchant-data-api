"""FX read-side logic: merge, as-of, convert, stats, compare. Pure logic: no I/O.

All money math uses ``Decimal`` on per-1-unit rates (``rate / unit``; PLAN.md D5).
The store supplies rows through the ``FxRateReader`` protocol.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Literal, Protocol

from imda.config import Settings
from imda.domain.calendar import CalendarDataMissing, HolidayCalendar
from imda.errors import InvalidInput, RangeTooLarge
from imda.models import IST, Currency, FxRate, Source

SourceChoice = Literal["auto", "rbi", "fbil"]
Period = Literal["week", "month"]

FBIL_START = dt.date(2018, 7, 10)
RBI_GAP = (dt.date(2018, 7, 25), dt.date(2022, 4, 11))
FX_CALENDAR_OFFICE = "mumbai"
MAX_RANGE_DAYS = 3660
FLAG_THRESHOLD_BPS = Decimal(1)
INR = "INR"

_CENT = Decimal("0.01")
_MEAN_PLACES = Decimal("1e-10")
_PCT_PLACES = Decimal("1e-6")
_BPS_PLACES = Decimal("1e-4")
_MIN_POINTS_FOR_VOLATILITY = 3
_SATURDAY = 5
_SUNDAY = 6


class FxRateReader(Protocol):
    def fx_rates(
        self, currency: Currency, start: dt.date, end: dt.date, source: Source | None = None
    ) -> list[FxRate]: ...  # date ASC; None = all sources

    def latest_fx_date(
        self, currency: Currency, source: Source | None = None
    ) -> dt.date | None: ...


class RateNotFound(LookupError):
    """No published rate within the as-of lookback window."""

    def __init__(self, currency: Currency, day: dt.date) -> None:
        self.currency = currency
        self.day = day
        super().__init__(f"No {currency.value} rate found on or before {day}")


@dataclass(frozen=True, slots=True)
class AsOfResult:
    currency: Currency
    requested_date: dt.date
    effective_date: dt.date
    rate: FxRate
    reason: str | None
    lag_days: int


@dataclass(frozen=True, slots=True)
class Conversion:
    amount: Decimal
    from_currency: str
    to_currency: str
    result: Decimal
    exact: Decimal
    rates_used: tuple[AsOfResult, ...]
    is_cross_rate: bool


@dataclass(frozen=True, slots=True)
class PeriodStats:
    period_start: dt.date
    period_end: dt.date
    count: int
    mean: Decimal
    min: Decimal
    max: Decimal
    first: Decimal
    last: Decimal
    change_pct: Decimal
    volatility: Decimal | None


@dataclass(frozen=True, slots=True)
class CompareRow:
    date: dt.date
    rbi: Decimal
    fbil: Decimal
    diff: Decimal
    diff_bps: Decimal
    flagged: bool


@dataclass(frozen=True, slots=True)
class CompareSummary:
    overlap_days: int
    flagged_days: int
    max_abs_diff_bps: Decimal
    rbi_only_days: int
    fbil_only_days: int


@dataclass(frozen=True, slots=True)
class CompareReport:
    currency: Currency
    start: dt.date
    end: dt.date
    rows: tuple[CompareRow, ...]
    summary: CompareSummary


def per_unit(rate: FxRate) -> Decimal:
    """INR value of exactly one unit of the currency."""
    return rate.rate / rate.unit


def check_range(start: dt.date, end: dt.date) -> None:
    """``end`` must not be before ``start``, and the span is capped at ``MAX_RANGE_DAYS``."""
    if end < start:
        raise InvalidInput(f"end {end} is before start {start}")
    days = (end - start).days
    if days > MAX_RANGE_DAYS:
        raise RangeTooLarge(days, MAX_RANGE_DAYS)


def _merge_auto(rows: list[FxRate]) -> list[FxRate]:
    """One row per date: FBIL from FBIL_START on, RBI before; the other as failover."""
    by_date: dict[dt.date, dict[Source, FxRate]] = {}
    for row in rows:
        by_date.setdefault(row.date, {})[row.source] = row
    merged: list[FxRate] = []
    for day in sorted(by_date):
        candidates = by_date[day]
        preferred, other = (
            (Source.FBIL, Source.RBI) if day >= FBIL_START else (Source.RBI, Source.FBIL)
        )
        merged.append(candidates[preferred] if preferred in candidates else candidates[other])
    return merged


def _quantize_cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _validate_amount(amount: Decimal) -> None:
    if not amount.is_finite() or amount <= 0:
        raise InvalidInput("amount must be a positive number")
    if amount != amount.quantize(_CENT):
        raise InvalidInput("amount must have at most 2 decimal places")


def _parse_currency(code: str) -> Currency | None:
    """``None`` for INR; ``InvalidInput`` for unsupported codes."""
    upper = code.upper()
    if upper == INR:
        return None
    try:
        return Currency(upper)
    except ValueError:
        raise InvalidInput(f"Unsupported currency: {code!r}") from None


class FxService:
    def __init__(
        self,
        reader: FxRateReader,
        *,
        calendar: HolidayCalendar | None,
        settings: Settings,
        now: Callable[[], dt.datetime],
    ) -> None:
        self._reader = reader
        self._calendar = calendar
        self._settings = settings
        self._now = now

    def rates(
        self, currency: Currency, start: dt.date, end: dt.date, source: SourceChoice = "auto"
    ) -> list[FxRate]:
        """Rates in ``[start, end]``, date ASC, one per date, each with its true source."""
        check_range(start, end)
        if source == "auto":
            return _merge_auto(self._reader.fx_rates(currency, start, end, None))
        rows = self._reader.fx_rates(currency, start, end, Source(source))
        return sorted(rows, key=lambda r: r.date)

    def as_of(self, currency: Currency, day: dt.date, source: SourceChoice = "auto") -> AsOfResult:
        """The rate in force on ``day``: its own row, else the latest earlier one."""
        window = min(self._settings.fx_asof_max_lookback_days, (day - dt.date.min).days)
        window_start = day - dt.timedelta(days=window)
        by_date = {r.date: r for r in self.rates(currency, window_start, day, source)}
        if day in by_date:
            return AsOfResult(currency, day, day, by_date[day], None, 0)
        for offset in range(1, window + 1):
            effective = day - dt.timedelta(days=offset)
            if effective in by_date:
                reason = self._missing_reason(day, source)
                return AsOfResult(currency, day, effective, by_date[effective], reason, offset)
        raise RateNotFound(currency, day)

    def convert(
        self,
        amount: Decimal,
        from_ccy: str,
        to_ccy: str,
        day: dt.date,
        source: SourceChoice = "auto",
    ) -> Conversion:
        """Convert between INR and a supported currency (or cross via INR).

        The result is rounded to 0.01 (ROUND_HALF_UP); ``exact`` keeps the
        unrounded ``Decimal``.
        """
        _validate_amount(amount)
        src, dst = _parse_currency(from_ccy), _parse_currency(to_ccy)
        if src == dst:
            raise InvalidInput("from and to currencies must differ")
        used: list[AsOfResult] = []
        exact = amount
        if src is not None:
            used.append(self.as_of(src, day, source))
            exact = exact * per_unit(used[-1].rate)
        if dst is not None:
            used.append(self.as_of(dst, day, source))
            exact = exact / per_unit(used[-1].rate)
        return Conversion(
            amount=amount,
            from_currency=from_ccy.upper(),
            to_currency=to_ccy.upper(),
            result=_quantize_cents(exact),
            exact=exact,
            rates_used=tuple(used),
            is_cross_rate=src is not None and dst is not None,
        )

    def stats(
        self,
        currency: Currency,
        start: dt.date,
        end: dt.date,
        period: Period,
        source: SourceChoice = "auto",
    ) -> list[PeriodStats]:
        """Per-period statistics on per-1-unit rates.

        Buckets are ISO weeks (Mon-Sun) or calendar months; ``period_start`` and
        ``period_end`` are the bucket boundaries. mean/min/max/first/last use
        ``Decimal`` (mean rounded to 10 dp, change_pct to 6 dp). ``volatility`` is
        the sample standard deviation of daily log returns, in percent: it is
        computed with ``float``/``math`` and returned as ``Decimal`` rounded to
        6 dp, or ``None`` with fewer than 3 points.
        """
        return self.stats_of(self.rates(currency, start, end, source), period)

    @staticmethod
    def stats_of(rates: list[FxRate], period: Period) -> list[PeriodStats]:
        """``stats`` over rows already fetched by ``rates`` (so a caller queries once)."""
        buckets: dict[dt.date, list[FxRate]] = {}
        for rate in rates:
            buckets.setdefault(_bucket_start(rate.date, period), []).append(rate)
        return [_period_stats(key, period, rows) for key, rows in sorted(buckets.items())]

    def compare(self, currency: Currency, start: dt.date, end: dt.date) -> CompareReport:
        """RBI against FBIL on dates where both published (per-1-unit values)."""
        check_range(start, end)
        rbi = {r.date: r for r in self._reader.fx_rates(currency, start, end, Source.RBI)}
        fbil = {r.date: r for r in self._reader.fx_rates(currency, start, end, Source.FBIL)}
        both = sorted(rbi.keys() & fbil.keys())
        rows = tuple(_compare_row(d, rbi[d], fbil[d]) for d in both)
        summary = CompareSummary(
            overlap_days=len(rows),
            flagged_days=sum(1 for r in rows if r.flagged),
            max_abs_diff_bps=max((abs(r.diff_bps) for r in rows), default=Decimal(0)),
            rbi_only_days=len(rbi.keys() - fbil.keys()),
            fbil_only_days=len(fbil.keys() - rbi.keys()),
        )
        return CompareReport(currency, start, end, rows, summary)

    def _missing_reason(self, day: dt.date, source: SourceChoice) -> str:
        """Why ``day`` itself has no rate (first match wins).

        A future date is "not yet published". For today, a weekend or holiday reason wins over
        "not yet published", because no rate will be published at all on a non-working day.
        """
        today = self._today_ist()
        if day > today:
            return "not yet published"
        weekday = day.weekday()
        if weekday == _SATURDAY:
            return "Saturday (no FX publication)"
        if weekday == _SUNDAY:
            return "Sunday"
        holiday = self._holiday_reason(day)
        if holiday is not None:
            return holiday
        if day == today and self._before_cutoff():
            return "not yet published"
        if source == "rbi" and RBI_GAP[0] <= day <= RBI_GAP[1]:
            return f"RBI did not publish reference rates between {RBI_GAP[0]} and {RBI_GAP[1]}"
        return "no publication on this date"

    def _now_ist(self) -> dt.datetime:
        now = self._now()
        if now.tzinfo is None:
            raise ValueError("now() must return a timezone-aware datetime")
        return now.astimezone(IST)

    def _today_ist(self) -> dt.date:
        return self._now_ist().date()

    def _before_cutoff(self) -> bool:
        cutoff = dt.time.fromisoformat(self._settings.fx_publish_cutoff_ist)
        return self._now_ist().time().replace(tzinfo=None) < cutoff

    def _holiday_reason(self, day: dt.date) -> str | None:
        if self._calendar is None:
            return None
        try:
            return self._calendar.non_working_reason(FX_CALENDAR_OFFICE, day)
        except CalendarDataMissing:
            return None


def _bucket_start(day: dt.date, period: Period) -> dt.date:
    if period == "week":
        return day - dt.timedelta(days=day.weekday())
    return day.replace(day=1)


def _bucket_end(start: dt.date, period: Period) -> dt.date:
    if period == "week":
        return start + dt.timedelta(days=6)
    next_month = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return next_month - dt.timedelta(days=1)


def _volatility(values: list[Decimal]) -> Decimal | None:
    """Sample stdev of daily log returns, in percent, rounded to 6 dp."""
    if len(values) < _MIN_POINTS_FOR_VOLATILITY:
        return None
    floats = [float(v) for v in values]
    returns = [math.log(b / a) for a, b in pairwise(floats)]
    return Decimal(repr(statistics.stdev(returns) * 100)).quantize(
        _PCT_PLACES, rounding=ROUND_HALF_UP
    )


def _period_stats(start: dt.date, period: Period, rows: list[FxRate]) -> PeriodStats:
    values = [per_unit(r) for r in rows]
    first, last = values[0], values[-1]
    return PeriodStats(
        period_start=start,
        period_end=_bucket_end(start, period),
        count=len(values),
        mean=(sum(values, Decimal(0)) / len(values)).quantize(_MEAN_PLACES, rounding=ROUND_HALF_UP),
        min=min(values),
        max=max(values),
        first=first,
        last=last,
        change_pct=((last - first) / first * 100).quantize(_PCT_PLACES, rounding=ROUND_HALF_UP),
        volatility=_volatility(values),
    )


def _compare_row(day: dt.date, rbi: FxRate, fbil: FxRate) -> CompareRow:
    rbi_value, fbil_value = per_unit(rbi), per_unit(fbil)
    diff = fbil_value - rbi_value
    bps = (diff / rbi_value * 10_000).quantize(_BPS_PLACES, rounding=ROUND_HALF_UP)
    return CompareRow(day, rbi_value, fbil_value, diff, bps, abs(bps) > FLAG_THRESHOLD_BPS)
