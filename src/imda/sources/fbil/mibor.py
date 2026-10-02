"""FBIL overnight MIBOR (percent per annum), JSON from ``ovnmibor``."""

from __future__ import annotations

import datetime as dt

from imda.models import Dataset, MiborRate, Source
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

MIBOR_PATH = "/ovnmibor/fetchfiltered"
_WHERE = (Source.FBIL, Dataset.MIBOR)
_REQUIRED = ("processRunDate", "tenor", "displayTime", "rate")


class FbilMiborAdapter:
    """Keeps every tenor FBIL returns: ``O/N`` normally, ``3D`` on Fridays (Friday to Monday)."""

    source: Source = Source.FBIL
    dataset: Dataset = Dataset.MIBOR

    def fetch(self, client: HttpClient, query: DateRangeQuery) -> list[RawPayload]:
        """One GET per window of at most 366 days."""
        return [client.send(build_request(MIBOR_PATH, window)) for window in chunk_ranges(query)]

    def parse(self, raw: RawPayload) -> list[MiborRate]:
        rates: dict[tuple[dt.date, str], MiborRate] = {}
        for index, row in enumerate(load_rows(raw, *_WHERE)):
            rate = _parse_row(row, index)
            key = (rate.date, rate.tenor)
            seen = rates.setdefault(key, rate)
            if seen.rate != rate.rate:
                raise ParseError(
                    *_WHERE,
                    f"conflicting duplicate rows for tenor {rate.tenor} on {rate.date}: "
                    f"{seen.rate} vs {rate.rate}",
                )
        return list(rates.values())

    def fingerprint(self, raw: RawPayload) -> dict[str, object]:
        return fingerprint_rows(load_rows(raw, *_WHERE), "tenor")


def _parse_row(row: Row, index: int) -> MiborRate:
    require(row, _REQUIRED, _WHERE, index)
    tenor = row["tenor"]
    if not isinstance(tenor, str) or not tenor.strip():
        raise ParseError(*_WHERE, f"row {index} tenor is not a non-empty string")
    return MiborRate(
        date=parse_date(row["processRunDate"], _WHERE, index),
        tenor=tenor.strip(),
        rate=parse_rate(row["rate"], _WHERE, index),
        published_at=parse_published_at(row["displayTime"], _WHERE, index),
    )
