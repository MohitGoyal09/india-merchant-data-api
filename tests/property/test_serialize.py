"""Properties of the JSON-safe serialisation helpers."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from imda.api.serialize import decimal_text, fx_row, to_jsonable
from imda.domain.fx_service import per_unit
from imda.models import Currency, FxRate, Source

finite_decimals = st.decimals(allow_nan=False, allow_infinity=False)
rates = st.builds(
    FxRate,
    currency=st.sampled_from(list(Currency)),
    date=st.dates(),
    rate=st.integers(min_value=1, max_value=10**9).map(lambda n: Decimal(n).scaleb(-4)),
    unit=st.sampled_from([1, 100, 10_000]),
    source=st.sampled_from(list(Source)),
    published_at=st.none() | st.datetimes(timezones=st.timezones()),
)


@given(finite_decimals)
def test_decimal_text_is_plain_notation_that_reads_back_exactly(value: Decimal) -> None:
    text = decimal_text(value)

    assert "e" not in text.lower()
    assert Decimal(text) == value
    assert Decimal(text).as_tuple().exponent == value.as_tuple().exponent or value == 0


@given(rates)
def test_fx_row_serialises_to_json_without_floats_and_keeps_the_per_unit_value(
    rate: FxRate,
) -> None:
    view = to_jsonable(fx_row(rate))

    assert json.loads(json.dumps(view), parse_float=_fail) == view
    assert Decimal(view["rate_per_unit"]) == per_unit(rate)
    assert Decimal(view["rate"]) == rate.rate
    assert dt.date.fromisoformat(view["date"]) == rate.date


def _fail(text: str) -> float:
    raise AssertionError(f"float literal in JSON: {text}")
