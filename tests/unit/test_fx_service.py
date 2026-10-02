"""Table-driven tests for FxService (in-memory fake reader, synthetic calendar)."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from decimal import Decimal
from itertools import pairwise
from typing import ClassVar

import pytest

from imda.config import Settings
from imda.domain.calendar import HolidayCalendar
from imda.domain.fx_service import (
    FBIL_START,
    FxService,
    RateNotFound,
    SourceChoice,
    check_range,
)
from imda.errors import InvalidInput, RangeTooLarge
from imda.models import IST, Currency, FxRate, Holiday, HolidayKind, Source

D = dt.date
DEC = Decimal
USD, EUR, JPY, IDR = Currency.USD, Currency.EUR, Currency.JPY, Currency.IDR
RBI, FBIL = Source.RBI, Source.FBIL
MUM = "mumbai"


class FakeReader:
    def __init__(self, rows: list[FxRate]) -> None:
        self.rows = rows

    def fx_rates(
        self, currency: Currency, start: dt.date, end: dt.date, source: Source | None = None
    ) -> list[FxRate]:
        found = [
            r
            for r in self.rows
            if r.currency == currency
            and start <= r.date <= end
            and (source is None or r.source == source)
        ]
        return sorted(found, key=lambda r: r.date)

    def latest_fx_date(self, currency: Currency, source: Source | None = None) -> dt.date | None:
        dates = [
            r.date
            for r in self.rows
            if r.currency == currency and (source is None or r.source == source)
        ]
        return max(dates, default=None)


def rate(
    day: dt.date,
    value: str,
    *,
    ccy: Currency = USD,
    source: Source = FBIL,
    unit: int = 1,
) -> FxRate:
    return FxRate(currency=ccy, date=day, rate=DEC(value), unit=unit, source=source)


def at(day: dt.date, hh: int, mm: int = 0) -> Callable[[], dt.datetime]:
    return lambda: dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


NOW = at(D(2026, 3, 10), 18)  # Tuesday, after the 13:30 IST cutoff


def calendar_2026() -> HolidayCalendar:
    return HolidayCalendar(
        [
            Holiday(
                office_slug=MUM, date=D(2026, 1, 26), name="Republic Day", kind=HolidayKind.NI_ACT
            )
        ],
        closing_of_accounts_is_holiday=True,
        loaded_years={MUM: frozenset({2026})},
    )


def service(
    rows: list[FxRate],
    *,
    calendar: HolidayCalendar | None = None,
    now: Callable[[], dt.datetime] = NOW,
    lookback: int = 10,
) -> FxService:
    return FxService(
        FakeReader(rows),
        calendar=calendar,
        settings=Settings(_env_file=None, fx_asof_max_lookback_days=lookback),
        now=now,
    )


class TestRatesAutoMerge:
    def test_fbil_preferred_from_fbil_start_rbi_before(self) -> None:
        rows = [
            rate(D(2018, 7, 9), "68.00", source=RBI),
            rate(D(2018, 7, 9), "69.00", source=FBIL),
            rate(FBIL_START, "68.50", source=RBI),
            rate(FBIL_START, "68.60", source=FBIL),
        ]
        got = service(rows).rates(USD, D(2018, 7, 9), FBIL_START)
        assert [(r.date, r.source, r.rate) for r in got] == [
            (D(2018, 7, 9), RBI, DEC("68.00")),
            (FBIL_START, FBIL, DEC("68.60")),
        ]

    @pytest.mark.parametrize(
        ("day", "present", "expected"),
        [
            (D(2024, 5, 6), RBI, RBI),  # FBIL era, FBIL missing -> RBI failover
            (D(2018, 7, 9), FBIL, FBIL),  # RBI era, RBI missing -> FBIL failover
        ],
    )
    def test_failover_keeps_true_source(
        self, day: dt.date, present: Source, expected: Source
    ) -> None:
        got = service([rate(day, "80", source=present)]).rates(USD, day, day)
        assert len(got) == 1
        assert got[0].source == expected

    def test_sorted_ascending_one_row_per_date(self) -> None:
        rows = [
            rate(D(2024, 5, 8), "83", source=FBIL),
            rate(D(2024, 5, 6), "81", source=RBI),
            rate(D(2024, 5, 7), "82", source=FBIL),
            rate(D(2024, 5, 7), "82.1", source=RBI),
        ]
        got = service(rows).rates(USD, D(2024, 5, 1), D(2024, 5, 31))
        assert [r.date for r in got] == [D(2024, 5, 6), D(2024, 5, 7), D(2024, 5, 8)]

    @pytest.mark.parametrize("choice", ["rbi", "fbil"])
    def test_single_source_only(self, choice: SourceChoice) -> None:
        rows = [
            rate(D(2024, 5, 7), "82", source=FBIL),
            rate(D(2024, 5, 7), "82.1", source=RBI),
            rate(D(2024, 5, 6), "81", source=FBIL),
        ]
        got = service(rows).rates(USD, D(2024, 5, 1), D(2024, 5, 31), choice)
        assert {r.source for r in got} == {Source(choice)}
        assert [r.date for r in got] == sorted(r.date for r in got)


class TestRangeGuard:
    def test_over_3660_days_rejected(self) -> None:
        with pytest.raises(InvalidInput, match="3660"):
            service([]).rates(USD, D(2010, 1, 1), D(2020, 2, 1))

    def test_exactly_3660_days_allowed(self) -> None:
        assert service([]).rates(USD, D(2020, 1, 1), D(2020, 1, 1) + dt.timedelta(days=3660)) == []

    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(InvalidInput, match="before"):
            service([]).rates(USD, D(2024, 2, 1), D(2024, 1, 1))

    def test_guard_applies_to_stats_and_compare(self) -> None:
        svc = service([])
        with pytest.raises(InvalidInput, match="3660"):
            svc.stats(USD, D(2000, 1, 1), D(2026, 1, 1), "month")
        with pytest.raises(InvalidInput, match="3660"):
            svc.compare(USD, D(2000, 1, 1), D(2026, 1, 1))


class TestAsOf:
    rows: ClassVar[list[FxRate]] = [
        rate(D(2026, 1, 22), "90.00"),
        rate(D(2026, 1, 23), "91.00"),
        rate(D(2026, 1, 27), "92.00"),
    ]

    def test_present_date_has_no_reason(self) -> None:
        res = service(self.rows).as_of(USD, D(2026, 1, 23))
        assert (res.effective_date, res.reason, res.lag_days) == (D(2026, 1, 23), None, 0)
        assert res.requested_date == D(2026, 1, 23)
        assert res.rate.rate == DEC("91.00")

    @pytest.mark.parametrize(
        ("day", "reason", "effective", "lag"),
        [
            (D(2026, 1, 24), "Saturday (no FX publication)", D(2026, 1, 23), 1),
            (D(2026, 1, 25), "Sunday", D(2026, 1, 23), 2),
            (D(2026, 1, 26), "Republic Day", D(2026, 1, 23), 3),
        ],
    )
    def test_weekend_and_mumbai_holiday_reasons(
        self, day: dt.date, reason: str, effective: dt.date, lag: int
    ) -> None:
        res = service(self.rows, calendar=calendar_2026()).as_of(USD, day)
        assert (res.reason, res.effective_date, res.lag_days) == (reason, effective, lag)

    def test_weekday_without_row_or_calendar(self) -> None:
        res = service(self.rows).as_of(USD, D(2026, 1, 26))
        assert res.reason == "no publication on this date"

    def test_today_before_cutoff_is_not_yet_published(self) -> None:
        rows = [rate(D(2026, 3, 9), "90.00")]
        res = service(rows, now=at(D(2026, 3, 10), 10, 0)).as_of(USD, D(2026, 3, 10))
        assert (res.reason, res.effective_date) == ("not yet published", D(2026, 3, 9))

    def test_today_holiday_beats_not_yet_published(self) -> None:
        # Real case found on 2026-10-02 (Gandhi Jayanti): no rate will ever be published today.
        rows = [rate(D(2026, 1, 23), "90.00")]
        res = service(rows, calendar=calendar_2026(), now=at(D(2026, 1, 26), 10, 0)).as_of(
            USD, D(2026, 1, 26)
        )
        assert (res.reason, res.effective_date) == ("Republic Day", D(2026, 1, 23))

    def test_today_after_cutoff_without_row_is_plain_missing(self) -> None:
        rows = [rate(D(2026, 3, 9), "90.00")]
        res = service(rows, now=at(D(2026, 3, 10), 14, 0)).as_of(USD, D(2026, 3, 10))
        assert res.reason == "no publication on this date"

    def test_cutoff_boundary_is_published(self) -> None:
        rows = [rate(D(2026, 3, 9), "90.00")]
        res = service(rows, now=at(D(2026, 3, 10), 13, 30)).as_of(USD, D(2026, 3, 10))
        assert res.reason == "no publication on this date"

    def test_future_date_not_yet_published(self) -> None:
        rows = [rate(D(2026, 3, 9), "90.00")]
        res = service(rows).as_of(USD, D(2026, 3, 12))
        assert (res.reason, res.effective_date, res.lag_days) == (
            "not yet published",
            D(2026, 3, 9),
            3,
        )

    def test_utc_now_is_converted_to_ist(self) -> None:
        # 2026-03-10 05:00 UTC == 10:30 IST, before the cutoff.
        utc_now = lambda: dt.datetime(2026, 3, 10, 5, 0, tzinfo=dt.UTC)  # noqa: E731
        rows = [rate(D(2026, 3, 9), "90.00")]
        res = service(rows, now=utc_now).as_of(USD, D(2026, 3, 10))
        assert res.reason == "not yet published"

    def test_naive_now_rejected(self) -> None:
        naive = lambda: dt.datetime(2026, 3, 10, 5, 0)  # noqa: E731
        with pytest.raises(ValueError, match="timezone-aware"):
            service([rate(D(2026, 3, 9), "90")], now=naive).as_of(USD, D(2026, 3, 10))

    def test_rbi_gap_reason_with_calendar_data_missing_fallthrough(self) -> None:
        rows = [rate(D(2020, 6, 2), "75.00", source=RBI)]
        res = service(rows, calendar=calendar_2026()).as_of(USD, D(2020, 6, 3), "rbi")
        assert res.reason == (
            "RBI did not publish reference rates between 2018-07-25 and 2022-04-11"
        )
        assert res.effective_date == D(2020, 6, 2)

    def test_rbi_gap_reason_only_for_rbi_source(self) -> None:
        rows = [rate(D(2020, 6, 2), "75.00", source=FBIL)]
        res = service(rows).as_of(USD, D(2020, 6, 3), "fbil")
        assert res.reason == "no publication on this date"

    def test_lookback_exhausted_raises_rate_not_found(self) -> None:
        rows = [rate(D(2026, 1, 1), "90.00")]
        with pytest.raises(RateNotFound) as exc:
            service(rows, lookback=5).as_of(USD, D(2026, 1, 20))
        assert exc.value.currency is USD
        assert exc.value.day == D(2026, 1, 20)

    def test_lookback_boundary_inclusive(self) -> None:
        rows = [rate(D(2026, 1, 15), "90.00")]
        assert service(rows, lookback=5).as_of(USD, D(2026, 1, 20)).lag_days == 5

    def test_auto_failover_in_as_of_keeps_source(self) -> None:
        rows = [rate(D(2026, 1, 23), "91.00", source=RBI)]
        res = service(rows).as_of(USD, D(2026, 1, 23))
        assert res.rate.source == RBI


class TestConvert:
    rows: ClassVar[list[FxRate]] = [
        rate(D(2026, 1, 23), "90.00", ccy=USD),
        rate(D(2026, 1, 23), "60.62", ccy=JPY, unit=100),
        rate(D(2026, 1, 23), "52.3", ccy=IDR, unit=10_000),
        rate(D(2026, 1, 23), "100.00", ccy=EUR),
    ]
    day = D(2026, 1, 23)

    @pytest.mark.parametrize(
        ("amount", "src", "dst", "result", "cross"),
        [
            ("10.00", "USD", "INR", "900.00", False),
            ("900", "INR", "USD", "10.00", False),
            ("1000", "JPY", "INR", "606.20", False),
            ("606.20", "INR", "JPY", "1000.00", False),
            ("10000", "IDR", "INR", "52.30", False),
            ("100", "usd", "eur", "90.00", True),
            ("100", "EUR", "USD", "111.11", True),
        ],
    )
    def test_conversions(self, amount: str, src: str, dst: str, result: str, cross: bool) -> None:
        conv = service(self.rows).convert(DEC(amount), src, dst, self.day)
        assert conv.result == DEC(result)
        assert conv.is_cross_rate is cross
        assert len(conv.rates_used) == (2 if cross else 1)
        assert (conv.from_currency, conv.to_currency) == (src.upper(), dst.upper())

    def test_exact_keeps_unrounded_value(self) -> None:
        conv = service(self.rows).convert(DEC("100"), "EUR", "USD", self.day)
        assert conv.result == DEC("111.11")
        assert isinstance(conv.exact, Decimal)
        assert str(conv.exact).startswith("111.1111111111")
        assert conv.amount == DEC("100")

    @pytest.mark.parametrize(
        ("amount", "result"),
        [("0.05", "0.03"), ("0.01", "0.01"), ("0.15", "0.08"), ("0.14", "0.07")],
    )
    def test_rounds_half_up(self, amount: str, result: str) -> None:
        rows = [rate(self.day, "0.5", ccy=USD)]  # 0.5 INR per USD
        conv = service(rows).convert(DEC(amount), "USD", "INR", self.day)
        assert conv.result == DEC(result)

    def test_uses_as_of_rate(self) -> None:
        conv = service(self.rows).convert(DEC("1"), "USD", "INR", D(2026, 1, 25))
        assert conv.rates_used[0].effective_date == self.day
        assert conv.rates_used[0].reason == "Sunday"

    @pytest.mark.parametrize(
        "amount",
        [
            DEC("0"),
            DEC("-1"),
            DEC("1.001"),
            DEC("NaN"),
            DEC("Infinity"),
            DEC("1E+30"),  # too many digits to quantize: must be InvalidInput, not InvalidOperation
            DEC("9" * 27),
            DEC("1250000000000000000000000"),  # valid itself, but the converted result overflows
        ],
    )
    def test_invalid_amounts(self, amount: Decimal) -> None:
        with pytest.raises(InvalidInput, match="amount"):
            service(self.rows).convert(amount, "USD", "INR", self.day)

    def test_trailing_zeros_beyond_two_places_are_fine(self) -> None:
        conv = service(self.rows).convert(DEC("1.500"), "USD", "INR", self.day)
        assert conv.result == DEC("135.00")

    @pytest.mark.parametrize(
        ("src", "dst"), [("INR", "INR"), ("USD", "USD"), ("USD", "XYZ"), ("ABC", "INR")]
    )
    def test_invalid_currencies(self, src: str, dst: str) -> None:
        with pytest.raises(InvalidInput, match=r"currenc"):
            service(self.rows).convert(DEC("1"), src, dst, self.day)

    def test_missing_rate_raises(self) -> None:
        with pytest.raises(RateNotFound):
            service([]).convert(DEC("1"), "USD", "INR", self.day)


class TestStats:
    def test_week_buckets_and_values(self) -> None:
        rows = [
            rate(D(2026, 3, 2), "100"),  # Mon
            rate(D(2026, 3, 3), "110"),
            rate(D(2026, 3, 4), "121"),
            rate(D(2026, 3, 9), "50"),  # next Mon
        ]
        got = service(rows).stats(USD, D(2026, 3, 1), D(2026, 3, 31), "week")
        assert [(s.period_start, s.period_end, s.count) for s in got] == [
            (D(2026, 3, 2), D(2026, 3, 8), 3),
            (D(2026, 3, 9), D(2026, 3, 15), 1),
        ]
        first = got[0]
        assert (first.mean, first.min, first.max) == (DEC("110.3333333333"), DEC("100"), DEC("121"))
        assert (first.first, first.last) == (DEC("100"), DEC("121"))
        assert first.change_pct == DEC("21.000000")
        assert first.volatility == DEC("0.000000")  # constant 10% returns -> stdev 0

    def test_volatility_matches_manual_log_return_stdev(self) -> None:
        values = [100.0, 101.0, 99.0, 102.0]
        rows = [rate(D(2026, 3, 2 + i), str(v)) for i, v in enumerate(values)]
        returns = [math.log(b / a) for a, b in pairwise(values)]
        mean = sum(returns) / len(returns)
        stdev = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) * 100
        got = service(rows).stats(USD, D(2026, 3, 1), D(2026, 3, 31), "week")[0]
        assert got.volatility == DEC(str(round(stdev, 6))).quantize(DEC("0.000001"))

    def test_month_buckets_use_calendar_boundaries(self) -> None:
        rows = [
            rate(D(2026, 1, 30), "90"),
            rate(D(2026, 2, 2), "91"),
            rate(D(2026, 2, 27), "92"),
            rate(D(2028, 2, 29), "93"),
        ]
        got = service(rows).stats(USD, D(2026, 1, 1), D(2026, 3, 1), "month")
        assert [(s.period_start, s.period_end, s.count) for s in got] == [
            (D(2026, 1, 1), D(2026, 1, 31), 1),
            (D(2026, 2, 1), D(2026, 2, 28), 2),
        ]
        leap = service(rows).stats(USD, D(2028, 2, 1), D(2028, 2, 29), "month")
        assert leap[0].period_end == D(2028, 2, 29)

    @pytest.mark.parametrize("n_points", [1, 2])
    def test_volatility_none_below_three_points(self, n_points: int) -> None:
        rows = [rate(D(2026, 3, 2 + i), "100") for i in range(n_points)]
        got = service(rows).stats(USD, D(2026, 3, 1), D(2026, 3, 31), "week")
        assert got[0].volatility is None

    def test_single_point_change_zero_and_unit_normalised(self) -> None:
        rows = [rate(D(2026, 3, 2), "60.62", ccy=JPY, unit=100)]
        got = service(rows).stats(JPY, D(2026, 3, 1), D(2026, 3, 31), "week")[0]
        assert got.change_pct == DEC("0")
        assert got.first == got.last == got.mean == DEC("0.6062")

    def test_decrease_gives_negative_change(self) -> None:
        rows = [rate(D(2026, 3, 2), "100"), rate(D(2026, 3, 3), "90")]
        got = service(rows).stats(USD, D(2026, 3, 1), D(2026, 3, 31), "week")[0]
        assert got.change_pct == DEC("-10.000000")

    def test_empty_range(self) -> None:
        assert service([]).stats(USD, D(2026, 3, 1), D(2026, 3, 31), "week") == []


class TestCompare:
    def test_flags_only_above_one_bps(self) -> None:
        rows = [
            rate(D(2024, 5, 6), "80.0000", source=RBI),
            rate(D(2024, 5, 6), "80.0080", source=FBIL),  # +1.0 bps -> not flagged
            rate(D(2024, 5, 7), "80.0000", source=RBI),
            rate(D(2024, 5, 7), "80.0100", source=FBIL),  # +1.25 bps -> flagged
            rate(D(2024, 5, 8), "80.0000", source=RBI),
            rate(D(2024, 5, 8), "79.9600", source=FBIL),  # -5 bps -> flagged
            rate(D(2024, 5, 9), "80.0000", source=RBI),  # RBI only
            rate(D(2024, 5, 10), "80.0000", source=FBIL),  # FBIL only
            rate(D(2024, 5, 11), "80.0000", source=FBIL),  # FBIL only
        ]
        rep = service(rows).compare(USD, D(2024, 5, 1), D(2024, 5, 31))
        assert [(r.date.day, r.diff_bps, r.flagged) for r in rep.rows] == [
            (6, DEC("1.0000"), False),
            (7, DEC("1.2500"), True),
            (8, DEC("-5.0000"), True),
        ]
        s = rep.summary
        assert (s.overlap_days, s.flagged_days, s.max_abs_diff_bps) == (3, 2, DEC("5.0000"))
        assert (s.rbi_only_days, s.fbil_only_days) == (1, 2)
        assert rep.rows[1].diff == DEC("0.0100")
        assert (rep.rows[1].rbi, rep.rows[1].fbil) == (DEC("80.0000"), DEC("80.0100"))

    def test_per_unit_normalisation(self) -> None:
        rows = [
            rate(D(2024, 5, 6), "53.00", ccy=JPY, unit=100, source=RBI),
            rate(D(2024, 5, 6), "53.00", ccy=JPY, unit=100, source=FBIL),
        ]
        rep = service(rows).compare(JPY, D(2024, 5, 1), D(2024, 5, 31))
        assert rep.rows[0].rbi == DEC("0.53")
        assert rep.rows[0].diff_bps == DEC("0.0000")
        assert not rep.rows[0].flagged

    def test_no_overlap(self) -> None:
        rep = service([rate(D(2024, 5, 6), "80", source=RBI)]).compare(
            USD, D(2024, 5, 1), D(2024, 5, 31)
        )
        assert rep.rows == ()
        assert rep.summary.max_abs_diff_bps == DEC("0")
        assert rep.summary.rbi_only_days == 1


class TestInputErrorsAreTyped:
    def test_range_too_large_is_an_invalid_input_with_details(self) -> None:
        with pytest.raises(RangeTooLarge) as exc:
            check_range(D(2000, 1, 1), D(2026, 1, 1))
        assert isinstance(exc.value, InvalidInput)
        assert exc.value.limit == 3660
        assert exc.value.days == (D(2026, 1, 1) - D(2000, 1, 1)).days

    def test_inverted_range_is_invalid_input(self) -> None:
        with pytest.raises(InvalidInput):
            check_range(D(2026, 1, 2), D(2026, 1, 1))

    def test_internal_naive_clock_stays_a_plain_value_error(self) -> None:
        naive = lambda: dt.datetime(2026, 3, 10, 5, 0)  # noqa: E731
        with pytest.raises(ValueError, match="timezone-aware") as exc:
            service([rate(D(2026, 3, 9), "90")], now=naive).as_of(USD, D(2026, 3, 10))
        assert not isinstance(exc.value, InvalidInput)


class TestAsOfAtTheDateFloor:
    def test_as_of_date_min_does_not_overflow(self) -> None:
        with pytest.raises(RateNotFound):
            service([]).as_of(USD, dt.date.min)


class TestExactIsADecimal:
    def test_exact_is_a_decimal_equal_to_the_unrounded_product(self) -> None:
        rows = [rate(D(2026, 3, 9), "90.00")]
        conv = service(rows).convert(DEC("1.50"), "USD", "INR", D(2026, 3, 9))
        assert isinstance(conv.exact, Decimal)
        assert conv.exact == DEC("135.00")


class TestStatsFromRates:
    def test_stats_of_matches_stats(self) -> None:
        rows = [rate(D(2026, 3, d), f"90.{d}") for d in (2, 3, 4, 5)]
        svc = service(rows)
        fetched = svc.rates(USD, D(2026, 3, 1), D(2026, 3, 31))
        direct = svc.stats(USD, D(2026, 3, 1), D(2026, 3, 31), "month")
        assert svc.stats_of(fetched, "month") == direct
