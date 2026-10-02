"""Shared pieces of the two FBIL JSON adapters (reference rates and MIBOR)."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from imda.models import IST, Dataset, Source
from imda.sources.base import DateRangeQuery, ParseError, RawPayload, UpstreamRequest

BASE_URL = "https://www.fbil.org.in/wasdm"
MAX_CHUNK_DAYS = 366
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_CODE_RE = re.compile(r"\b[A-Z]{3}\b")
_DIGITS_RE = re.compile(r"\d+")

Row = Mapping[str, Any]


def chunk_ranges(query: DateRangeQuery, max_days: int = MAX_CHUNK_DAYS) -> Iterator[DateRangeQuery]:
    """Split ``query`` into consecutive inclusive ranges of at most ``max_days`` days."""
    start = query.start
    while start <= query.end:
        end = min(start + dt.timedelta(days=max_days - 1), query.end)
        yield DateRangeQuery(start, end)
        start = end + dt.timedelta(days=1)


def build_request(path: str, window: DateRangeQuery) -> UpstreamRequest:
    """GET request for one window. FBIL answers HTTP 500 to non-ISO dates, so only ISO is sent."""
    return UpstreamRequest(
        method="GET",
        url=f"{BASE_URL}{path}",
        params={
            "fromDate": window.start.isoformat(),
            "toDate": window.end.isoformat(),
            "authenticated": "false",
        },
    )


def load_rows(raw: RawPayload, source: Source, dataset: Dataset) -> list[Row]:
    """Decode the body into a list of JSON objects, or raise ``ParseError``."""
    try:
        data = json.loads(raw.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ParseError(source, dataset, f"body is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ParseError(source, dataset, f"expected a JSON list, got {type(data).__name__}")
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise ParseError(source, dataset, f"row {index} is {type(row).__name__}, not an object")
    return data


def require(row: Row, keys: tuple[str, ...], where: tuple[Source, Dataset], index: int) -> None:
    missing = [key for key in keys if key not in row]
    if missing:
        raise ParseError(*where, f"row {index} is missing keys {missing}")


def parse_date(value: object, where: tuple[Source, Dataset], index: int) -> dt.date:
    return _parse_timestamp(value, where, index, "processRunDate").date()


def parse_published_at(value: object, where: tuple[Source, Dataset], index: int) -> dt.datetime:
    """``displayTime`` is wall-clock time in India; return it as an IST-aware datetime."""
    return _parse_timestamp(value, where, index, "displayTime").replace(tzinfo=IST)


def _parse_timestamp(
    value: object, where: tuple[Source, Dataset], index: int, key: str
) -> dt.datetime:
    if not isinstance(value, str):
        raise ParseError(*where, f"row {index} {key} is not a string")
    try:
        return dt.datetime.strptime(value, _TIMESTAMP_FORMAT)  # noqa: DTZ007
    except ValueError as exc:
        raise ParseError(*where, f"row {index} {key} is not 'YYYY-MM-DD HH:MM:SS'") from exc


def parse_rate(value: object, where: tuple[Source, Dataset], index: int) -> Decimal:
    """Convert via ``str`` so the JSON float's shortest repr is kept; never store a float."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ParseError(*where, f"row {index} rate has type {type(value).__name__}")
    try:
        rate = Decimal(str(value))
    except InvalidOperation as exc:
        raise ParseError(*where, f"row {index} rate is not a number") from exc
    if not rate.is_finite():
        raise ParseError(*where, f"row {index} rate is not finite")
    return rate


def label_pattern(label: object) -> str:
    """Structure of a label with data stripped: ``INR / 100 JPY`` -> ``CCC / N CCC``."""
    return _DIGITS_RE.sub("N", _CODE_RE.sub("CCC", str(label)))


def fingerprint_rows(rows: list[Row], label_key: str) -> dict[str, object]:
    """Structural summary: key sets, value types per key, label-pattern count. No values."""
    key_sets = sorted({tuple(sorted(row)) for row in rows})
    types: dict[str, set[str]] = {}
    for row in rows:
        for key, value in row.items():
            types.setdefault(key, set()).add(type(value).__name__)
    patterns = Counter(label_pattern(row[label_key]) for row in rows if label_key in row)
    return {
        "row_count": len(rows),
        "key_sets": [list(keys) for keys in key_sets],
        "value_types": {key: sorted(names) for key, names in sorted(types.items())},
        "label_pattern_count": len(patterns),
    }
