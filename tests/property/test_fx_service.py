"""Properties of FxService over random synthetic rate series with gaps."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from imda.config import Settings
from imda.domain.fx_service import FBIL_START, FxService, RateNotFound, SourceChoice, per_unit
from imda.errors import InvalidInput
from imda.models import IST, Currency, FxRate, Source

CENT = Decimal("0.01")
NOW = dt.datetime(2030, 1, 2, 18, 0, tzinfo=IST)
WINDOW_DAYS = 60
UNITS = (1, 100, 10_000)

rate_values = st.integers(min_value=1, max_value=5_000_000).map(lambda n: Decimal(n) / 10_000)
amounts = st.integers(min_value=1, max_value=10**11).map(lambda n: Decimal(n) / 100)
currencies = st.sampled_from(list(Currency))
source_choices: st.SearchStrategy[SourceChoice] = st.sampled_from(["auto", "rbi", "fbil"])


class FakeReader:
    def __init__(self, rows: list[FxRate]) -> None:
        self._rows = rows

    def fx_rates(
        self, currency: Currency, start: dt.date, end: dt.date, source: Source | None = None
    ) -> list[FxRate]:
        found = [
            r
            for r in self._rows
            if r.currency == currency
            and start <= r.date <= end
            and (source is None or r.source == source)
        ]
        return sorted(found, key=lambda r: r.date)

    def latest_fx_date(self, currency: Currency, source: Source | None = None) -> dt.date | None:
        dates = [
            r.date
            for r in self._rows
            if r.currency == currency and (source is None or r.source == source)
        ]
        return max(dates, default=None)


@st.composite
def series(draw: st.DrawFn, currency: Currency = Currency.USD) -> tuple[dt.date, list[FxRate]]:
    """A window start plus rows for both sources, with gaps; may straddle FBIL_START."""
    start = FBIL_START - dt.timedelta(days=draw(st.integers(0, WINDOW_DAYS)))
    unit = draw(st.sampled_from(UNITS))
    keys = draw(
        st.sets(st.tuples(st.integers(0, WINDOW_DAYS), st.sampled_from(list(Source))), max_size=80)
    )
    rows = [
        FxRate(
            currency=currency,
            date=start + dt.timedelta(days=offset),
            rate=draw(rate_values),
            unit=unit,
            source=source,
        )
        for offset, source in sorted(keys)
    ]
    return start, rows


def make_service(rows: list[FxRate], lookback: int = 10) -> FxService:
    return FxService(
        FakeReader(rows),
        calendar=None,
        settings=Settings(_env_file=None, fx_asof_max_lookback_days=lookback),
        now=lambda: NOW,
    )


@given(series(), st.data())
def test_auto_returns_one_row_per_date_sorted_with_fbil_preferred_from_its_start(
    window: tuple[dt.date, list[FxRate]], data: st.DataObject
) -> None:
    start, rows = window
    first = data.draw(st.integers(0, WINDOW_DAYS))
    last = data.draw(st.integers(first, WINDOW_DAYS))
    lo, hi = start + dt.timedelta(days=first), start + dt.timedelta(days=last)

    got = make_service(rows).rates(Currency.USD, lo, hi, "auto")

    by_date: dict[dt.date, dict[Source, FxRate]] = {}
    for row in rows:
        if lo <= row.date <= hi:
            by_date.setdefault(row.date, {})[row.source] = row
    assert [r.date for r in got] == sorted(by_date)
    for row in got:
        offered = by_date[row.date]
        preferred = Source.FBIL if row.date >= FBIL_START else Source.RBI
        assert row.source == (preferred if preferred in offered else next(iter(offered)))
        assert row == offered[row.source]


@given(series(), source_choices, st.integers(1, 60), st.data())
def test_as_of_picks_the_latest_row_on_or_before_the_date_within_the_lookback(
    window: tuple[dt.date, list[FxRate]], source: SourceChoice, lookback: int, data: st.DataObject
) -> None:
    start, rows = window
    day = start + dt.timedelta(days=data.draw(st.integers(0, WINDOW_DAYS + 20)))
    service = make_service(rows, lookback)
    available = {r.date for r in service.rates(Currency.USD, start, day, source)}
    reachable = [d for d in available if 0 <= (day - d).days <= lookback]

    if not reachable:
        with pytest.raises(RateNotFound):
            service.as_of(Currency.USD, day, source)
        return
    result = service.as_of(Currency.USD, day, source)

    assert result.effective_date == max(reachable)
    assert result.effective_date <= day
    assert result.lag_days == (day - result.effective_date).days <= lookback
    assert (result.effective_date == day) == (day in available)
    assert (result.reason is None) == (result.lag_days == 0)
    assert result.rate.date == result.effective_date


@given(
    currencies,
    st.sampled_from(UNITS),
    rate_values,
    amounts,
    st.sampled_from(["auto", "rbi", "fbil"]),
)
def test_foreign_to_inr_uses_the_per_unit_rate(
    currency: Currency, unit: int, value: Decimal, amount: Decimal, source: SourceChoice
) -> None:
    day = dt.date(2024, 5, 6)
    row = FxRate(currency=currency, date=day, rate=value, unit=unit, source=Source.FBIL)
    service = make_service([row])
    if source == "rbi":
        with pytest.raises(RateNotFound):
            service.convert(amount, currency.value, "INR", day, source)
        return

    conversion = service.convert(amount, currency.value, "INR", day, source)

    assert conversion.exact == amount * value / unit
    assert conversion.result == conversion.exact.quantize(CENT, rounding=ROUND_HALF_UP)
    assert not conversion.is_cross_rate


@given(currencies, st.sampled_from(UNITS), rate_values, amounts)
def test_foreign_to_inr_to_foreign_round_trips_within_quantisation_error(
    currency: Currency, unit: int, value: Decimal, amount: Decimal
) -> None:
    day = dt.date(2024, 5, 6)
    row = FxRate(currency=currency, date=day, rate=value, unit=unit, source=Source.FBIL)
    service = make_service([row])
    pu = per_unit(row)

    inr = service.convert(amount, currency.value, "INR", day).result
    assume(inr > 0)  # a sub-paisa intermediate is (rightly) rejected as a zero amount
    back = service.convert(inr, "INR", currency.value, day).result

    # The INR leg is rounded to 0.01, which is worth 0.005/pu of the foreign currency.
    assert abs(back - amount) <= CENT + Decimal("0.005") / pu
    if pu >= 1:
        assert abs(back - amount) <= CENT


@given(currencies, st.sampled_from(UNITS), rate_values, amounts)
def test_inr_to_foreign_to_inr_round_trips_within_quantisation_error(
    currency: Currency, unit: int, value: Decimal, amount: Decimal
) -> None:
    day = dt.date(2024, 5, 6)
    row = FxRate(currency=currency, date=day, rate=value, unit=unit, source=Source.FBIL)
    service = make_service([row])
    pu = per_unit(row)

    foreign = service.convert(amount, "INR", currency.value, day).result
    assume(foreign > 0)
    back = service.convert(foreign, currency.value, "INR", day).result

    assert abs(back - amount) <= CENT + Decimal("0.005") * pu
    if pu <= 1:
        assert abs(back - amount) <= CENT


@given(st.decimals(allow_nan=True, allow_infinity=True), currencies)
def test_convert_rejects_bad_amounts_with_invalid_input_only(
    amount: Decimal, currency: Currency
) -> None:
    day = dt.date(2024, 5, 6)
    row = FxRate(currency=currency, date=day, rate=Decimal("80"), unit=1, source=Source.FBIL)
    service = make_service([row])

    try:
        conversion = service.convert(amount, currency.value, "INR", day)
    except InvalidInput:
        return
    assert amount > 0
    assert amount == amount.quantize(CENT)
    assert conversion.result >= 0
