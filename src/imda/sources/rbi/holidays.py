"""RBI bank-holiday adapter for ``HolidayMatrixDisplay.aspx`` (ASP.NET WebForms).

The page answers in two layouts:

* ``month_matrix``: one month, all offices. Header row ``["March 2026", "3", "19", ...]``
  lists only holiday days; each office row has a marker per day. A second table maps
  day to holiday name.
* ``office_list``: one office, flat list with a ``dop_header`` row per month, then
  ``<tr><td>day</td><td>name</td><td>marker</td></tr>`` rows.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from collections.abc import Mapping, Sequence

from selectolax.parser import HTMLParser, Node

from imda.models import Dataset, Holiday, HolidayKind, Source
from imda.sources.base import HolidayQuery, HttpClient, ParseError, RawPayload
from imda.sources.rbi.aspnet import (
    PostbackSession,
    clean_text,
    extract_select_options,
    form_field_names,
)
from imda.sources.rbi.offices import ALL_OFFICES_VALUE, OFFICE_SELECT, slugify

HOLIDAYS_URL = "https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx"
LAYOUT_MATRIX = "month_matrix"
LAYOUT_LIST = "office_list"
LAYOUT_EMPTY = "no_holidays"
NI_ACT_GLYPH = "•"
CLOSING_GLYPH = "■"
YEAR_SELECT = "drYear"
_MONTHS = {name: number for number, name in enumerate(calendar.month_name) if name}
_MATRIX_HEADER = re.compile(r"^([A-Za-z]+) (\d{4})$")
_DESCRIPTION_HEADER = ["Holiday Description", "Day"]
_NO_HOLIDAYS = re.compile(r"There are no holidays in (?:([A-Za-z]+) )?(\d{4})")


def _fail(reason: str) -> ParseError:
    return ParseError(Source.RBI, Dataset.HOLIDAYS, reason)


def _month_number(name: str) -> int:
    month = _MONTHS.get(name)
    if month is None:
        raise _fail(f"unknown month name {name!r}")
    return month


def _make_date(year: int, month: int, day: int) -> dt.date:
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise _fail(f"invalid date {year}-{month:02d}-{day}") from exc


def _parse_day(text: str) -> int:
    if not text.isdigit():
        raise _fail(f"expected a day number, got {text!r}")
    return int(text)


def _marker_kind(cell: Node) -> HolidayKind | None:
    """Classify a marker cell; ``None`` when the cell is empty (not a holiday)."""
    if not clean_text(cell):
        return None
    glyph, style, hidden = "", "", ""
    for span in cell.css("span"):
        if "HideText" in (span.attributes.get("class") or ""):
            hidden = clean_text(span).lower()
        elif not glyph:
            glyph, style = clean_text(span), (span.attributes.get("style") or "").lower()
    if glyph == CLOSING_GLYPH or "firebrick" in style or "closing of accounts" in hidden:
        return HolidayKind.CLOSING_OF_ACCOUNTS
    # The hidden legend text is authoritative; glyphs vary by year (e.g. "▲" in 2007 means
    # "Holiday under Negotiable Instruments Act and Real Time Gross Settlement Holiday").
    if glyph == NI_ACT_GLYPH or "negotiable instruments act" in hidden:
        return HolidayKind.NI_ACT
    if "real time gross settlement" in hidden:
        # RTGS-only holiday ("◆"): RTGS is closed but banks are open, so it is not a bank
        # holiday for business-day or settlement purposes. Documented in LIMITATIONS.md.
        return None
    raise _fail(f"unknown marker {glyph!r} ({hidden or 'no legend text'})")


def _is_rtgs_only(cell: Node) -> bool:
    hidden = " ".join(
        clean_text(span).lower()
        for span in cell.css("span")
        if "HideText" in (span.attributes.get("class") or "")
    )
    return "real time gross settlement" in hidden and "negotiable instruments act" not in hidden


def _office_slugs(html: str) -> dict[str, str]:
    options = extract_select_options(
        html, OFFICE_SELECT, source=Source.RBI, dataset=Dataset.HOLIDAYS
    )
    return {value: slugify(label) for value, label in options if value != ALL_OFFICES_VALUE}


def _header_texts(table: Node) -> list[str]:
    first_row = table.css_first("tr")
    return [clean_text(th) for th in first_row.css("th")] if first_row else []


def _find_matrix(tree: HTMLParser) -> Node | None:
    for table in tree.css("table"):
        header = _header_texts(table)
        if header and _MATRIX_HEADER.match(header[0]):
            return table
    return None


def _day_names(tree: HTMLParser) -> dict[int, str]:
    for table in tree.css("table"):
        if _header_texts(table) != _DESCRIPTION_HEADER:
            continue
        names: dict[int, list[str]] = {}
        for row in table.css("tr")[1:]:
            cells = row.css("td")
            if len(cells) != 2:
                raise _fail("description row does not have 2 cells")
            names.setdefault(_parse_day(clean_text(cells[1])), []).append(clean_text(cells[0]))
        return {day: "/".join(parts) for day, parts in names.items()}
    raise _fail("holiday description table not found")


def _parse_matrix(tree: HTMLParser, matrix: Node) -> list[Holiday]:
    header = _header_texts(matrix)
    found = _MATRIX_HEADER.match(header[0])
    if found is None:  # pragma: no cover - guarded by _find_matrix
        raise _fail("matrix header lost")
    month, year = _month_number(found.group(1)), int(found.group(2))
    days = [_parse_day(text) for text in header[1:]]
    names = _day_names(tree)
    holidays: list[Holiday] = []
    for row in matrix.css("tr")[1:]:
        cells = row.css("td")
        if not cells:
            continue
        if len(cells) - 1 != len(days):
            raise _fail(f"matrix row has {len(cells) - 1} cells, header has {len(days)} days")
        office = slugify(clean_text(cells[0]))
        for day, cell in zip(days, cells[1:], strict=True):
            kind = _marker_kind(cell)
            if kind is None:
                continue
            if day not in names:
                raise _fail(f"no description for day {day}")
            holidays.append(
                Holiday(
                    office_slug=office,
                    date=_make_date(year, month, day),
                    name=names[day],
                    kind=kind,
                )
            )
    return holidays


def _form_int(form: Mapping[str, str] | None, key: str) -> int:
    value = (form or {}).get(key, "")
    if not value.isdigit():
        raise _fail(f"request form lacks numeric {key!r}")
    return int(value)


def _office_list_rows(tree: HTMLParser) -> list[Node]:
    return [
        row
        for table in tree.css("table")
        if table.css_first("span.dop_header") is not None
        for row in table.css("tr")
    ]


def _parse_office_list(
    tree: HTMLParser, form: Mapping[str, str] | None, slugs: Mapping[str, str]
) -> list[Holiday]:
    year = _form_int(form, YEAR_SELECT)
    office_id = str(_form_int(form, OFFICE_SELECT))
    if office_id not in slugs:
        raise _fail(f"office id {office_id} is not in the dropdown")
    holidays: list[Holiday] = []
    month: int | None = None
    for row in _office_list_rows(tree):
        month_header = row.css_first("span.dop_header")
        if month_header is not None:
            month = _month_number(clean_text(month_header))
            continue
        cells = row.css("td")
        if len(cells) != 3 or month is None:
            raise _fail("office-list row is not 'day, name, marker' under a month header")
        kind, name = _marker_kind(cells[2]), clean_text(cells[1])
        if kind is None and _is_rtgs_only(cells[2]):
            continue
        if kind is None or not name:
            raise _fail("office-list row has no marker or no name")
        day = _parse_day(clean_text(cells[0]))
        holidays.append(
            Holiday(
                office_slug=slugs[office_id],
                date=_make_date(year, month, day),
                name=name,
                kind=kind,
            )
        )
    return holidays


def _has_office_list_title(tree: HTMLParser) -> bool:
    title = tree.css_first("h3.sub_title")
    return title is not None and "holiday list for the year" in clean_text(title).lower()


def _no_holidays_period(tree: HTMLParser) -> tuple[int | None, int] | None:
    """(month or None, year) when RBI says "There are no holidays in <Month> <Year>"."""
    body = tree.body
    match = _NO_HOLIDAYS.search(clean_text(body)) if body is not None else None
    if match is None:
        return None
    month = _month_number(match.group(1)) if match.group(1) else None
    return month, int(match.group(2))


def _check_empty_matches_request(
    period: tuple[int | None, int], form: Mapping[str, str] | None
) -> None:
    month, year = period
    requested_month = _form_int(form, "drMonth") or None
    if (month, year) != (requested_month, _form_int(form, "drYear")):
        raise _fail(f"'no holidays' notice for {month}/{year} does not match the request")


def _check_complete(html: str) -> None:
    if "</html>" not in html.lower():
        raise _fail("document is truncated (no closing </html>)")


class RbiHolidayAdapter:
    source = Source.RBI
    dataset = Dataset.HOLIDAYS

    def __init__(self) -> None:
        self._session = PostbackSession(HOLIDAYS_URL, source=Source.RBI, dataset=Dataset.HOLIDAYS)

    def fetch(self, client: HttpClient, query: HolidayQuery) -> Sequence[RawPayload]:
        fields = {
            "drRegionalOffice": str(query.office_rbi_id or 0),
            "drMonth": str(query.month or 0),
            "drYear": str(query.year),
            "btnGo": "GO",
        }
        return [self._session.post(client, fields)]

    def parse(self, raw: RawPayload) -> list[Holiday]:
        html = raw.text()
        _check_complete(html)
        slugs = _office_slugs(html)
        tree = HTMLParser(html)
        matrix = _find_matrix(tree)
        if matrix is not None:
            return _parse_matrix(tree, matrix)
        if tree.css_first("span.dop_header") is not None or _has_office_list_title(tree):
            return _parse_office_list(tree, raw.request.form, slugs)
        empty = _no_holidays_period(tree)
        if empty is not None:
            _check_empty_matches_request(empty, raw.request.form)
            return []
        raise _fail("no holiday matrix or office list found")

    def fingerprint(self, raw: RawPayload) -> dict[str, object]:
        html = raw.text()
        _check_complete(html)
        tree = HTMLParser(html)
        if _find_matrix(tree) is not None:
            layout, shape = LAYOUT_MATRIX, ["th:month-year", "th:day*", "td:office", "td:marker*"]
        elif tree.css_first("span.dop_header") is not None or _has_office_list_title(tree):
            layout, shape = LAYOUT_LIST, ["th:month", "td:day", "td:name", "td:marker"]
        elif _no_holidays_period(tree) is not None:
            layout, shape = LAYOUT_EMPTY, ["p:no-holidays-notice"]
        else:
            raise _fail("no holiday matrix or office list found")
        return {
            "layout": layout,
            "form_fields": form_field_names(html),
            "select_option_counts": _select_option_counts(tree),
            "table_shape": shape,
        }


def _select_option_counts(tree: HTMLParser) -> dict[str, int]:
    """Option counts per dropdown; the year list is skipped because it grows every January."""
    counts = {
        select.attributes.get("name") or "": len(select.css("option"))
        for select in tree.css("select")
    }
    if OFFICE_SELECT not in counts:
        raise _fail(f"select {OFFICE_SELECT!r} not found")
    return {name: n for name, n in sorted(counts.items()) if name and name != YEAR_SELECT}
