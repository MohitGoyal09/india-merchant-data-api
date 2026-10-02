"""Argument parsing. Reuses the REST layer's validators so both surfaces accept the same input."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, TypeAdapter, ValidationError

from imda.api.deps import AmountText, AwareDatetime, ConvertCurrency, CurrencyCode, IsoDate
from imda.api.routes.fx import decode_cursor
from imda.errors import InvalidInput
from imda.mcp.errors import McpToolError, invalid_request
from imda.models import Currency

_DATE = TypeAdapter(IsoDate)
_AWARE = TypeAdapter(AwareDatetime)
_AMOUNT = TypeAdapter(AmountText)
_CURRENCY = TypeAdapter(CurrencyCode)
_CONVERT = TypeAdapter(ConvertCurrency)
_VALUE_ERROR_PREFIX = "Value error, "
_FORMAT_HINT = "Fix the argument and call the tool again."


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


# Schema-visible enums. Case-insensitive on input; a bad value becomes INVALID_REQUEST.
ForeignCurrency = Annotated[
    Literal["USD", "GBP", "EUR", "JPY", "AED", "IDR"],
    BeforeValidator(_upper),
    Field(description="Currency code: USD, GBP, EUR, JPY, AED or IDR."),
]
AnyCurrency = Annotated[
    Literal["INR", "USD", "GBP", "EUR", "JPY", "AED", "IDR"],
    BeforeValidator(_upper),
    Field(description="INR or one of USD, GBP, EUR, JPY, AED, IDR."),
]
Source = Annotated[
    Literal["auto", "rbi", "fbil"],
    Field(description="'auto' (default): FBIL from 2018-07-10, RBI before. Or force 'rbi'/'fbil'."),
]


def _message(exc: ValidationError) -> str:
    first = exc.errors()[0]["msg"]
    return first.removeprefix(_VALUE_ERROR_PREFIX)


def _parse[T](adapter: TypeAdapter[T], field: str, value: object) -> T:
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        raise invalid_request(f"{field}: {_message(exc)}", _FORMAT_HINT) from None


def parse_date(field: str, value: str) -> dt.date:
    """``YYYY-MM-DD``, a real date between 2000-01-01 and 2100-12-31."""
    return _parse(_DATE, field, value)


def parse_aware_datetime(field: str, value: str) -> dt.datetime:
    """ISO 8601 with a UTC offset, e.g. ``2026-03-27T11:00:00+05:30``."""
    return _parse(_AWARE, field, value)


def parse_amount(field: str, value: str) -> Decimal:
    """A decimal string. Sign and decimal places are checked by the domain (VALIDATION_ERROR)."""
    return _parse(_AMOUNT, field, value)


def parse_currency(field: str, value: str) -> Currency:
    return _parse(_CURRENCY, field, value)


def parse_convert_currency(field: str, value: str) -> str:
    return _parse(_CONVERT, field, value)


def parse_cursor(value: str) -> dt.date:
    try:
        return decode_cursor(value)
    except InvalidInput:
        raise invalid_request(
            "cursor: not a cursor from a previous fetch_all_fx_rates result",
            "Pass next_cursor exactly as returned, or omit cursor to start from from_date.",
        ) from None


def parse_range(
    from_field: str, from_value: str, to_field: str, to_value: str
) -> tuple[dt.date, dt.date]:
    return parse_date(from_field, from_value), parse_date(to_field, to_value)


def normalise_office(value: str) -> str:
    """``' New Delhi '`` -> ``'new-delhi'``."""
    return "-".join(value.strip().lower().split())


__all__ = [
    "AnyCurrency",
    "ForeignCurrency",
    "McpToolError",
    "Source",
    "normalise_office",
    "parse_amount",
    "parse_aware_datetime",
    "parse_convert_currency",
    "parse_currency",
    "parse_cursor",
    "parse_date",
    "parse_range",
]
