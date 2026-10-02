"""FBIL reference rates (INR per N units of a foreign currency), JSON from ``refrates``."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ValidationError

from imda.models import Currency, Dataset, FxRate, Source
from imda.sources.base import DateRangeQuery, HttpClient, ParseError, RawPayload
from imda.sources.fbil.common import (
    Row,
    build_request,
    chunk_ranges,
    fingerprint_rows,
    load_rows,
    parse_date,
    parse_published_at,
    parse_rate,
    require,
)

FX_PATH = "/refrates/fetchfiltered"
_WHERE = (Source.FBIL, Dataset.FX)
_REQUIRED = ("processRunDate", "subProdName", "displayTime", "rate")
# "INR / 1 USD", "INR / 100 JPY", and the 2021 variant "INR/1 USD" (no spaces)
_LABEL_RE = re.compile(r"INR\s*/\s*(\d+)\s*([A-Z]{3})")


@dataclass(frozen=True, slots=True)
class FxParseReport:
    """Parsed rates plus the currency codes we deliberately left out (e.g. ``RUB``)."""

    rates: tuple[FxRate, ...]
    skipped_codes: Mapping[str, int] = field(default_factory=dict)


class FbilFxAdapter:
    source: Source = Source.FBIL
    dataset: Dataset = Dataset.FX

    def fetch(self, client: HttpClient, query: DateRangeQuery) -> list[RawPayload]:
        """One GET per window of at most 366 days."""
        return [client.send(build_request(FX_PATH, window)) for window in chunk_ranges(query)]

    def parse(self, raw: RawPayload) -> list[FxRate]:
        return list(self.parse_with_report(raw).rates)

    def parse_with_report(self, raw: RawPayload) -> FxParseReport:
        rows = load_rows(raw, *_WHERE)
        rates: dict[tuple[Currency, dt.date], FxRate] = {}
        skipped: Counter[str] = Counter()
        for index, row in enumerate(rows):
            rate = _parse_row(row, index, skipped)
            if rate is not None:
                _add_unique(rates, rate)
        return FxParseReport(rates=tuple(rates.values()), skipped_codes=dict(skipped))

    def fingerprint(self, raw: RawPayload) -> dict[str, object]:
        return fingerprint_rows(load_rows(raw, *_WHERE), "subProdName")


def _parse_row(row: Row, index: int, skipped: Counter[str]) -> FxRate | None:
    require(row, _REQUIRED, _WHERE, index)
    label = row["subProdName"]
    match = _LABEL_RE.fullmatch(label.strip()) if isinstance(label, str) else None
    if match is None:
        raise ParseError(*_WHERE, f"row {index} has an unrecognised subProdName")
    unit, code = int(match.group(1)), match.group(2)
    try:
        currency = Currency(code)
    except ValueError:
        skipped[code] += 1
        return None
    try:
        return FxRate(
            currency=currency,
            date=parse_date(row["processRunDate"], _WHERE, index),
            rate=parse_rate(row["rate"], _WHERE, index),
            unit=unit,
            source=Source.FBIL,
            published_at=parse_published_at(row["displayTime"], _WHERE, index),
        )
    except ValidationError as exc:
        raise ParseError(*_WHERE, f"row {index} is invalid: {_first_error(exc)}") from exc


def _add_unique(rates: dict[tuple[Currency, dt.date], FxRate], rate: FxRate) -> None:
    key = (rate.currency, rate.date)
    seen = rates.get(key)
    if seen is None:
        rates[key] = rate
    elif (seen.unit, seen.rate) != (rate.unit, rate.rate):
        raise ParseError(
            *_WHERE,
            f"conflicting duplicate rows for {rate.currency} on {rate.date}: "
            f"{_describe(seen.rate, seen.unit)} vs {_describe(rate.rate, rate.unit)}",
        )


def _describe(rate: Decimal, unit: int) -> str:
    return f"{rate} per {unit}"


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    return f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
