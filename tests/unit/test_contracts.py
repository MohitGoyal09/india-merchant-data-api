import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from imda.models import Currency, FxRate, Office, Source
from imda.sources.base import DateRangeQuery, HolidayQuery


def test_fx_rate_is_immutable_and_keeps_unit():
    rate = FxRate(
        currency=Currency.JPY,
        date=dt.date(2026, 9, 24),
        rate=Decimal("60.62"),
        unit=100,
        source=Source.FBIL,
    )
    with pytest.raises(ValidationError):
        rate.rate = Decimal("1")  # type: ignore[misc]
    assert rate.unit == 100


def test_fx_rate_rejects_non_positive_rate():
    with pytest.raises(ValidationError):
        FxRate(
            currency=Currency.USD,
            date=dt.date(2026, 1, 1),
            rate=Decimal("0"),
            unit=1,
            source=Source.RBI,
        )


def test_office_slug_must_be_kebab_case():
    with pytest.raises(ValidationError):
        Office(rbi_id=28, slug="New Delhi", name="New Delhi")


def test_date_range_rejects_inverted_range():
    with pytest.raises(ValueError, match="after end"):
        DateRangeQuery(start=dt.date(2026, 2, 1), end=dt.date(2026, 1, 1))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"year": 2026, "month": 13, "office_rbi_id": 28}, "month must be 1-12"),
        ({"year": 2026}, "all-offices"),
    ],
)
def test_holiday_query_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        HolidayQuery(**kwargs)


def test_holiday_query_allows_all_offices_for_one_month():
    assert HolidayQuery(year=2026, month=10).office_rbi_id is None
