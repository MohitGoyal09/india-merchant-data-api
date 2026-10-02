"""RBI reference-rate adapter for ``ReferenceRateArchive.aspx`` (ASP.NET WebForms)."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator, Sequence
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from selectolax.parser import HTMLParser, Node

from imda.models import Currency, Dataset, FxRate, Source
from imda.sources.base import DateRangeQuery, HttpClient, ParseError, RawPayload
from imda.sources.rbi.aspnet import PostbackSession, clean_text, form_field_names

FX_URL = "https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx"
MAX_CHUNK_DAYS = 366
DATE_FORMAT = "%d/%m/%Y"
CHECKBOXES = ("chkAll", "chkUSD", "chkGBP", "chkEURO", "chkYEN", "chkAED", "chkIDR")
NO_DATA_TEXT = "no reference rate found"
LAYOUT_TABLE = "rate_table"
LAYOUT_NO_DATA = "no_data"
_HEADER = re.compile(r"^([A-Z]{3}) \(INR / (\d+) ([A-Z]{3})\)$")


def _fail(reason: str) -> ParseError:
    return ParseError(Source.RBI, Dataset.FX, reason)


def _chunks(start: dt.date, end: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
    """Yield contiguous inclusive ranges of at most ``MAX_CHUNK_DAYS`` days."""
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=MAX_CHUNK_DAYS - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + dt.timedelta(days=1)


def _find_rate_table(tree: HTMLParser) -> Node | None:
    for table in tree.css("table"):
        first = table.css_first("tr")
        cells = first.css("td, th") if first else []
        if cells and clean_text(cells[0]) == "Date" and len(cells) > 1:
            return table
    return None


def _has_no_data_notice(tree: HTMLParser) -> bool:
    return any(NO_DATA_TEXT in clean_text(table).lower() for table in tree.css("table.tablebg"))


def _check_complete(html: str) -> None:
    if "</html>" not in html.lower():
        raise _fail("document is truncated (no closing </html>)")


def _header_labels(table: Node) -> list[str]:
    first = table.css_first("tr")
    return [clean_text(cell) for cell in first.css("td, th")] if first else []


def _columns(labels: Sequence[str]) -> list[tuple[Currency, int]]:
    """Map header labels after ``Date`` to ``(currency, unit)``; unknown columns raise."""
    columns: list[tuple[Currency, int]] = []
    for label in labels:
        found = _HEADER.match(label)
        if found is None:
            raise _fail(f"unrecognised column header {label!r}")
        code, unit, quoted = found.groups()
        if code != quoted:
            raise _fail(f"column header {label!r} mixes currencies {code} and {quoted}")
        try:
            columns.append((Currency(code), int(unit)))
        except ValueError as exc:
            raise _fail(f"unknown currency column {code!r}") from exc
    return columns


def _parse_date(text: str) -> dt.date:
    try:
        return dt.datetime.strptime(text, DATE_FORMAT).date()  # noqa: DTZ007 - date only
    except ValueError as exc:
        raise _fail(f"bad date {text!r}") from exc


def _make_rate(currency: Currency, unit: int, day: dt.date, text: str) -> FxRate:
    try:
        return FxRate(currency=currency, date=day, rate=Decimal(text), unit=unit, source=Source.RBI)
    except (InvalidOperation, ValidationError) as exc:
        raise _fail(f"bad {currency} rate {text!r} on {day}") from exc


def _parse_row(row: Node, columns: Sequence[tuple[Currency, int]]) -> list[FxRate]:
    cells = [clean_text(td) for td in row.css("td")]
    if len(cells) != len(columns) + 1:
        raise _fail(f"row has {len(cells)} cells, header has {len(columns) + 1}")
    day = _parse_date(cells[0])
    return [
        _make_rate(currency, unit, day, text)
        for (currency, unit), text in zip(columns, cells[1:], strict=True)
        if text
    ]


class RbiFxAdapter:
    source = Source.RBI
    dataset = Dataset.FX

    def __init__(self) -> None:
        self._session = PostbackSession(FX_URL, source=Source.RBI, dataset=Dataset.FX)

    def fetch(self, client: HttpClient, query: DateRangeQuery) -> Sequence[RawPayload]:
        payloads: list[RawPayload] = []
        for start, end in _chunks(query.start, query.end):
            fields = {box: "on" for box in CHECKBOXES}
            fields["txtFromDate"] = start.strftime(DATE_FORMAT)
            fields["txtToDate"] = end.strftime(DATE_FORMAT)
            fields["btnSubmit"] = " GO "
            payloads.append(self._session.post(client, fields))
        return payloads

    def parse(self, raw: RawPayload) -> list[FxRate]:
        html = raw.text()
        _check_complete(html)
        tree = HTMLParser(html)
        table = _find_rate_table(tree)
        if table is None:
            if _has_no_data_notice(tree):
                return []
            raise _fail("rate result table not found")
        columns = _columns(_header_labels(table)[1:])
        rates = [rate for row in table.css("tr")[1:] for rate in _parse_row(row, columns)]
        return sorted(rates, key=lambda r: (r.date, r.currency.value))

    def fingerprint(self, raw: RawPayload) -> dict[str, object]:
        html = raw.text()
        _check_complete(html)
        tree = HTMLParser(html)
        table = _find_rate_table(tree)
        if table is not None:
            layout, header = LAYOUT_TABLE, _header_labels(table)
        elif _has_no_data_notice(tree):
            layout, header = LAYOUT_NO_DATA, []
        else:
            raise _fail("rate result table not found")
        return {
            "layout": layout,
            "form_fields": form_field_names(html),
            "table_header": header,
        }
