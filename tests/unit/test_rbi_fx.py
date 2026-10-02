"""Tests for the RBI reference-rate adapter, driven by recorded fixtures."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from imda.models import Currency, Dataset, FxRate, Source
from imda.sources.base import (
    DateRangeQuery,
    ParseError,
    RawPayload,
    UpstreamError,
    UpstreamRequest,
)
from imda.sources.rbi.fx import FX_URL, MAX_CHUNK_DAYS, RbiFxAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "rbi"
NOW = dt.datetime(2026, 10, 2, tzinfo=dt.UTC)


def load_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def make_payload(body: bytes, form: dict[str, str] | None = None) -> RawPayload:
    return RawPayload(
        request=UpstreamRequest(method="POST", url=FX_URL, form=form),
        status_code=200,
        body=body,
        content_type="text/html",
        fetched_at=NOW,
        sha256=hashlib.sha256(body).hexdigest(),
        duration_ms=1,
    )


def load_payload(name: str) -> RawPayload:
    meta = json.loads((FIXTURES / f"{name}.meta.json").read_text(encoding="utf-8"))
    return make_payload((FIXTURES / f"{name}.html").read_bytes(), meta["form"] or None)


def parse_html(html: str) -> list[FxRate]:
    return RbiFxAdapter().parse(make_payload(html.encode()))


def rate_of(rates: list[FxRate], currency: Currency, date: dt.date) -> FxRate:
    (match,) = [r for r in rates if r.currency is currency and r.date == date]
    return match


# ---------------------------------------------------------------- parse


def test_parse_september_2026_yields_every_currency_for_every_published_day() -> None:
    rates = RbiFxAdapter().parse(load_payload("fx_2026_09"))

    assert len(rates) == 21 * 6
    assert {r.currency for r in rates} == set(Currency)
    assert {r.source for r in rates} == {Source.RBI}
    assert {r.published_at for r in rates} == {None}


def test_parse_matches_verified_values_for_2026_09_30() -> None:
    rates = RbiFxAdapter().parse(load_payload("fx_2026_09"))
    day = dt.date(2026, 9, 30)

    assert rate_of(rates, Currency.USD, day).rate == Decimal("95.9832")
    assert rate_of(rates, Currency.GBP, day).rate == Decimal("127.1886")
    assert rate_of(rates, Currency.EUR, day).rate == Decimal("108.9368")
    assert rate_of(rates, Currency.AED, day).rate == Decimal("26.1332")


def test_parse_reads_units_from_the_table_header() -> None:
    rates = RbiFxAdapter().parse(load_payload("fx_2026_09"))
    day = dt.date(2026, 9, 30)

    assert {c: rate_of(rates, c, day).unit for c in Currency} == {
        Currency.USD: 1,
        Currency.GBP: 1,
        Currency.EUR: 1,
        Currency.JPY: 100,
        Currency.AED: 1,
        Currency.IDR: 10000,
    }
    assert rate_of(rates, Currency.JPY, day).rate == Decimal("61.1600")
    assert rate_of(rates, Currency.IDR, day).rate == Decimal("53.7190")


def test_parse_keeps_decimal_precision_and_never_uses_float() -> None:
    rates = RbiFxAdapter().parse(load_payload("fx_2026_09"))

    assert all(isinstance(r.rate, Decimal) for r in rates)
    assert str(rate_of(rates, Currency.JPY, dt.date(2026, 9, 30)).rate) == "61.1600"


def test_parse_returns_oldest_first_and_is_sorted_deterministically() -> None:
    rates = RbiFxAdapter().parse(load_payload("fx_2026_09"))

    keys = [(r.date, r.currency.value) for r in rates]
    assert keys == sorted(keys)
    assert rates[0].date == dt.date(2026, 9, 1)


def test_parse_skips_empty_cells_in_the_old_era() -> None:
    rates = RbiFxAdapter().parse(load_payload("fx_2018_07"))

    assert {r.currency for r in rates} == {Currency.USD, Currency.GBP, Currency.EUR, Currency.JPY}
    assert len(rates) == 7 * 4
    assert max(r.date for r in rates) == dt.date(2018, 7, 24)
    assert rate_of(rates, Currency.USD, dt.date(2018, 7, 24)).rate == Decimal("69.0530")


def test_parse_gap_period_returns_empty_list() -> None:
    assert RbiFxAdapter().parse(load_payload("fx_2019_01_gap")) == []


def test_parse_page_without_result_table_raises() -> None:
    with pytest.raises(ParseError, match="result"):
        parse_html(load_html("fx_page"))


def test_parse_empty_and_truncated_bodies_raise() -> None:
    html = load_html("fx_2026_09")

    with pytest.raises(ParseError):
        parse_html("")
    with pytest.raises(ParseError):
        parse_html(html[: len(html) // 2])


def test_parse_unknown_header_column_raises() -> None:
    html = load_html("fx_2026_09").replace("AED (INR / 1 AED)", "CHF (INR / 1 CHF)")

    with pytest.raises(ParseError, match="CHF"):
        parse_html(html)


def test_parse_unreadable_header_column_raises() -> None:
    html = load_html("fx_2026_09").replace("AED (INR / 1 AED)", "Dirham rate")

    with pytest.raises(ParseError, match="Dirham"):
        parse_html(html)


def test_parse_header_whose_two_currency_codes_differ_raises() -> None:
    html = load_html("fx_2026_09").replace("AED (INR / 1 AED)", "AED (INR / 1 USD)")

    with pytest.raises(ParseError, match="AED"):
        parse_html(html)


def test_parse_bad_date_raises() -> None:
    html = load_html("fx_2026_09").replace("30/09/2026", "31/09/2026")

    with pytest.raises(ParseError, match="31/09/2026"):
        parse_html(html)


def test_parse_non_numeric_rate_raises() -> None:
    html = load_html("fx_2026_09").replace("95.9832", "n/a")

    with pytest.raises(ParseError, match="n/a"):
        parse_html(html)


def test_parse_non_positive_rate_raises() -> None:
    html = load_html("fx_2026_09").replace("95.9832", "0.0000")

    with pytest.raises(ParseError, match=r"0\.0000"):
        parse_html(html)


def test_parse_row_with_wrong_cell_count_raises() -> None:
    html = load_html("fx_2026_09").replace('<td height="20" align="right">53.7190</td>', "", 1)

    with pytest.raises(ParseError, match="cells"):
        parse_html(html)


def test_parse_is_a_pure_function_of_the_payload() -> None:
    adapter = RbiFxAdapter()
    payload = load_payload("fx_2026_09")

    assert adapter.parse(payload) == adapter.parse(payload)


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_is_stable_across_different_date_ranges() -> None:
    adapter = RbiFxAdapter()

    assert adapter.fingerprint(load_payload("fx_2026_09")) == adapter.fingerprint(
        load_payload("fx_2018_07")
    )


def test_fingerprint_lists_header_columns_and_form_fields_but_no_values() -> None:
    fp = RbiFxAdapter().fingerprint(load_payload("fx_2026_09"))

    assert fp["layout"] == "rate_table"
    assert fp["table_header"] == [
        "Date",
        "USD (INR / 1 USD)",
        "GBP (INR / 1 GBP)",
        "EUR (INR / 1 EUR)",
        "JPY (INR / 100 JPY)",
        "AED (INR / 1 AED)",
        "IDR (INR / 10000 IDR)",
    ]
    assert "chkYEN" in fp["form_fields"]  # type: ignore[operator]
    assert "95.9832" not in json.dumps(fp)
    assert "2026" not in json.dumps(fp)


def test_fingerprint_changes_when_a_column_is_renamed() -> None:
    adapter = RbiFxAdapter()
    renamed = load_html("fx_2026_09").replace("IDR (INR / 10000 IDR)", "IDR (INR / 1000 IDR)")

    assert adapter.fingerprint(make_payload(renamed.encode())) != adapter.fingerprint(
        load_payload("fx_2026_09")
    )


def test_fingerprint_for_empty_period_reports_no_data_layout() -> None:
    fp = RbiFxAdapter().fingerprint(load_payload("fx_2019_01_gap"))

    assert fp["layout"] == "no_data"
    assert fp["table_header"] == []


def test_fingerprint_raises_on_garbage() -> None:
    with pytest.raises(ParseError):
        RbiFxAdapter().fingerprint(make_payload(b"<html></html>"))


# ---------------------------------------------------------------- fetch


class FakeClient:
    """Serves the recorded FX page for GET and a chosen fixture for POST."""

    def __init__(self, post_fixture: str = "fx_2026_09", *, fail_posts: int = 0) -> None:
        self.requests: list[UpstreamRequest] = []
        self._post_fixture = post_fixture
        self._fail_posts = fail_posts

    def send(self, request: UpstreamRequest) -> RawPayload:
        self.requests.append(request)
        if request.method == "POST" and self._fail_posts > 0:
            self._fail_posts -= 1
            raise UpstreamError("HTTP 500", url=request.url, status_code=500)
        name = "fx_page" if request.method == "GET" else self._post_fixture
        data = (FIXTURES / f"{name}.html").read_bytes()
        return RawPayload(
            request=request,
            status_code=200,
            body=data,
            content_type="text/html",
            fetched_at=NOW,
            sha256=hashlib.sha256(data).hexdigest(),
            duration_ms=1,
        )

    def posts(self) -> list[dict[str, str]]:
        return [dict(r.form or {}) for r in self.requests if r.method == "POST"]


def d(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso)


def test_fetch_single_month_gets_once_then_posts_all_currencies() -> None:
    client = FakeClient()

    payloads = RbiFxAdapter().fetch(client, DateRangeQuery(d("2026-09-01"), d("2026-09-30")))

    assert [r.method for r in client.requests] == ["GET", "POST"]
    (form,) = client.posts()
    assert form["txtFromDate"] == "01/09/2026"
    assert form["txtToDate"] == "30/09/2026"
    assert form["btnSubmit"] == " GO "
    assert {
        form[box] for box in ("chkAll", "chkUSD", "chkGBP", "chkEURO", "chkYEN", "chkAED", "chkIDR")
    } == {"on"}
    assert form["__VIEWSTATE"].startswith("/wEP")
    assert form["__EVENTVALIDATION"]
    assert [p.request.method for p in payloads] == ["POST"]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-09-15", "2026-09-15", [("15/09/2026", "15/09/2026")]),
        ("2020-01-01", "2020-12-31", [("01/01/2020", "31/12/2020")]),  # 366 days
        (
            "2021-01-01",
            "2022-01-01",  # 366 days, non-leap start
            [("01/01/2021", "01/01/2022")],
        ),
        (
            "2021-01-01",
            "2022-01-02",  # 367 days
            [("01/01/2021", "01/01/2022"), ("02/01/2022", "02/01/2022")],
        ),
        (
            "2020-01-01",
            "2021-12-31",
            [("01/01/2020", "31/12/2020"), ("01/01/2021", "31/12/2021")],
        ),
    ],
)
def test_fetch_splits_ranges_into_chunks_of_at_most_366_days(
    start: str, end: str, expected: list[tuple[str, str]]
) -> None:
    client = FakeClient()

    RbiFxAdapter().fetch(client, DateRangeQuery(d(start), d(end)))

    assert [(f["txtFromDate"], f["txtToDate"]) for f in client.posts()] == expected
    assert sum(r.method == "GET" for r in client.requests) == 1


def test_fetch_full_history_uses_contiguous_chunks_within_the_limit() -> None:
    client = FakeClient()

    RbiFxAdapter().fetch(client, DateRangeQuery(d("2000-01-03"), d("2026-09-30")))

    spans = [
        (
            dt.datetime.strptime(f["txtFromDate"], "%d/%m/%Y").date(),
            dt.datetime.strptime(f["txtToDate"], "%d/%m/%Y").date(),
        )
        for f in client.posts()
    ]
    assert spans[0][0] == d("2000-01-03")
    assert spans[-1][1] == d("2026-09-30")
    assert all((end - start).days + 1 <= MAX_CHUNK_DAYS for start, end in spans)
    assert all(nxt[0] == prev[1] + dt.timedelta(days=1) for prev, nxt in pairwise(spans))
    assert len(spans) == 27


def test_fetch_retries_once_when_cached_state_is_rejected() -> None:
    adapter = RbiFxAdapter()
    adapter.fetch(FakeClient(), DateRangeQuery(d("2026-09-01"), d("2026-09-30")))
    client = FakeClient(fail_posts=1)

    adapter.fetch(client, DateRangeQuery(d("2026-08-01"), d("2026-08-31")))

    assert [r.method for r in client.requests] == ["POST", "GET", "POST"]


def test_fetch_then_parse_round_trip() -> None:
    client = FakeClient()
    adapter = RbiFxAdapter()

    rates = [
        rate
        for raw in adapter.fetch(client, DateRangeQuery(d("2026-09-01"), d("2026-09-30")))
        for rate in adapter.parse(raw)
    ]

    assert len(rates) == 126


def test_adapter_identity() -> None:
    adapter = RbiFxAdapter()

    assert (adapter.source, adapter.dataset) == (Source.RBI, Dataset.FX)
