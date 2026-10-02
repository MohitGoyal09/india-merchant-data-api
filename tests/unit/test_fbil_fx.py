from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from imda.models import IST, Currency, Dataset, Source
from imda.sources.base import DateRangeQuery, ParseError, RawPayload, UpstreamRequest
from imda.sources.fbil.fx import FbilFxAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "fbil"
URL = "https://www.fbil.org.in/wasdm/refrates/fetchfiltered"


def raw_of(body: bytes | str | object) -> RawPayload:
    payload = (
        body
        if isinstance(body, bytes)
        else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
    )
    return RawPayload(
        request=UpstreamRequest(method="GET", url=URL),
        status_code=200,
        body=payload,
        content_type="application/json",
        fetched_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
        sha256="0" * 64,
        duration_ms=1,
    )


def fixture(name: str) -> RawPayload:
    return raw_of((FIXTURES / f"{name}.json").read_bytes())


def row(
    label: str = "INR / 1 USD",
    day: str = "2026-09-24",
    rate: object = 95.9099,
    shown: str | None = None,
) -> dict[str, object]:
    return {
        "processRunDate": f"{day} 00:00:00",
        "subProdName": label,
        "displayTime": shown or f"{day} 13:00:00",
        "rate": rate,
        "comments": "",
    }


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[UpstreamRequest] = []

    def send(self, request: UpstreamRequest) -> RawPayload:
        self.requests.append(request)
        return raw_of([])


adapter = FbilFxAdapter()


def test_identity() -> None:
    assert adapter.source is Source.FBIL
    assert adapter.dataset is Dataset.FX


# -- fetch ----------------------------------------------------------------------------------


def test_fetch_single_window_sends_iso_dates() -> None:
    client = FakeClient()

    adapter.fetch(client, DateRangeQuery(dt.date(2026, 9, 1), dt.date(2026, 9, 30)))

    assert len(client.requests) == 1
    req = client.requests[0]
    assert req.method == "GET"
    assert req.url == URL
    assert dict(req.params) == {
        "fromDate": "2026-09-01",
        "toDate": "2026-09-30",
        "authenticated": "false",
    }


def test_fetch_splits_long_ranges_into_windows_of_at_most_366_days() -> None:
    client = FakeClient()
    query = DateRangeQuery(dt.date(2018, 7, 10), dt.date(2026, 9, 24))

    payloads = adapter.fetch(client, query)

    windows = [
        (dt.date.fromisoformat(r.params["fromDate"]), dt.date.fromisoformat(r.params["toDate"]))
        for r in client.requests
    ]
    assert len(payloads) == len(windows) == 9
    assert windows[0][0] == query.start
    assert windows[-1][1] == query.end
    assert all((end - start).days + 1 <= 366 for start, end in windows)
    assert all(b[0] == a[1] + dt.timedelta(days=1) for a, b in pairwise(windows))


def test_fetch_exactly_366_days_is_one_request_and_367_is_two() -> None:
    client = FakeClient()
    start = dt.date(2020, 1, 1)

    adapter.fetch(client, DateRangeQuery(start, start + dt.timedelta(days=365)))
    assert len(client.requests) == 1
    adapter.fetch(client, DateRangeQuery(start, start + dt.timedelta(days=366)))
    assert len(client.requests) == 3


# -- parse: recorded fixtures ---------------------------------------------------------------


def test_parse_recorded_september_2026_skips_rub() -> None:
    report = adapter.parse_with_report(fixture("fx_2026_09"))

    assert report.skipped_codes == {"RUB": 8}
    assert len(report.rates) == 102
    assert {r.currency for r in report.rates} == set(Currency)
    assert all(r.source is Source.FBIL for r in report.rates)


def test_parse_keeps_units_decimals_and_ist_publication_time() -> None:
    rates = adapter.parse(fixture("fx_2026_09"))

    by_key = {(r.currency, r.date): r for r in rates}
    usd = by_key[(Currency.USD, dt.date(2026, 9, 24))]
    assert usd.rate == Decimal("95.9099")
    assert usd.unit == 1
    assert usd.published_at == dt.datetime(2026, 9, 24, 13, 0, tzinfo=IST)
    assert by_key[(Currency.JPY, dt.date(2026, 9, 24))].unit == 100
    assert by_key[(Currency.IDR, dt.date(2026, 9, 1))].unit == 10000
    assert all(isinstance(r.rate, Decimal) for r in rates)


def test_parse_2021_includes_no_space_usd_label() -> None:
    rates = adapter.parse(fixture("fx_2021"))

    usd = [r for r in rates if r.currency is Currency.USD]
    assert len(usd) == 241  # 229 "INR / 1 USD" + 12 "INR/1 USD"
    assert all(r.unit == 1 for r in usd)
    assert {r.currency for r in rates} == {Currency.USD, Currency.GBP, Currency.EUR, Currency.JPY}


def test_parse_start_of_data_2018() -> None:
    rates = adapter.parse(fixture("fx_2018_07"))

    assert len(rates) == 64
    assert min(r.date for r in rates) == dt.date(2018, 7, 10)
    assert rates[0].published_at == dt.datetime(2018, 7, 31, 13, 30, tzinfo=IST)


# -- parse: synthetic edge cases ------------------------------------------------------------


def test_float_is_converted_through_str_not_binary() -> None:
    [rate] = adapter.parse(raw_of([row(rate=0.1)]))

    assert rate.rate == Decimal("0.1")


def test_integer_and_string_rates_are_accepted() -> None:
    rates = adapter.parse(raw_of([row(rate=96), row(label="INR / 1 GBP", rate="127.5")]))

    assert [r.rate for r in rates] == [Decimal("96"), Decimal("127.5")]


def test_identical_duplicate_rows_collapse_to_one() -> None:
    rates = adapter.parse(raw_of([row("INR / 1 USD"), row("INR/1 USD")]))

    assert len(rates) == 1


def test_conflicting_duplicate_rows_are_an_error() -> None:
    with pytest.raises(ParseError, match="conflicting duplicate"):
        adapter.parse(raw_of([row(rate=95.9), row("INR/1 USD", rate=96.0)]))


def test_tolerant_label_whitespace() -> None:
    rates = adapter.parse(raw_of([row("INR/1   USD"), row(" INR /100 JPY ", day="2026-09-23")]))

    assert [(r.currency, r.unit) for r in rates] == [(Currency.USD, 1), (Currency.JPY, 100)]


def test_unknown_currency_is_skipped_and_counted() -> None:
    report = adapter.parse_with_report(
        raw_of([row("INR / 1 RUB"), row("INR / 1 CNY"), row("INR / 1 RUB", day="2026-09-23")])
    )

    assert report.rates == ()
    assert report.skipped_codes == {"RUB": 2, "CNY": 1}


def test_empty_list_parses_to_nothing() -> None:
    report = adapter.parse_with_report(raw_of([]))

    assert report.rates == ()
    assert report.skipped_codes == {}


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<html>500</html>", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        ({"error": "x"}, "expected a JSON list"),
        (["x"], "not an object"),
    ],
)
def test_bad_json_shapes_raise_parse_error(body: object, reason: str) -> None:
    with pytest.raises(ParseError, match=reason) as exc:
        adapter.parse(raw_of(body))

    assert exc.value.source is Source.FBIL
    assert exc.value.dataset is Dataset.FX


@pytest.mark.parametrize("missing", ["processRunDate", "subProdName", "displayTime", "rate"])
def test_missing_required_key_raises(missing: str) -> None:
    bad = row()
    del bad[missing]

    with pytest.raises(ParseError, match=missing):
        adapter.parse(raw_of([bad]))


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"subProdName": "USD"}, "unrecognised subProdName"),
        ({"subProdName": 7}, "unrecognised subProdName"),
        ({"rate": None}, "rate has type"),
        ({"rate": True}, "rate has type"),
        ({"rate": "abc"}, "not a number"),
        ({"rate": "NaN"}, "not finite"),
        ({"rate": 0}, "invalid"),
        ({"rate": -1.5}, "invalid"),
        ({"processRunDate": "24/09/2026"}, "processRunDate is not"),
        ({"processRunDate": 20260924}, "processRunDate is not a string"),
        ({"displayTime": "13:00"}, "displayTime is not"),
        ({"subProdName": "INR / 0 USD"}, "invalid"),
    ],
)
def test_malformed_row_values_raise(patch: dict[str, object], reason: str) -> None:
    with pytest.raises(ParseError, match=reason):
        adapter.parse(raw_of([{**row(), **patch}]))


# -- fingerprint ----------------------------------------------------------------------------


def test_fingerprint_is_structural_and_ignores_values() -> None:
    fp_a = adapter.fingerprint(fixture("fx_2021"))
    fp_b = adapter.fingerprint(raw_of([row("INR / 1 USD", rate=1.5), row("INR/1 USD", rate=2.5)]))

    assert fp_a["key_sets"] == [
        ["comments", "displayTime", "processRunDate", "rate", "subProdName"]
    ]
    assert fp_a["value_types"] == {
        "comments": ["str"],
        "displayTime": ["str"],
        "processRunDate": ["str"],
        "rate": ["float"],
        "subProdName": ["str"],
    }
    assert fp_a["label_pattern_count"] == 2  # "INR / N CCC" and "INR/N CCC"
    assert fp_a["row_count"] == 964
    assert fp_b["label_pattern_count"] == 2
    assert fp_b["key_sets"] == fp_a["key_sets"]
    text = json.dumps(fp_a)
    assert "95." not in text
    assert "2026" not in text


def test_fingerprint_detects_drift() -> None:
    drifted = adapter.fingerprint(raw_of([{**row(), "rate": "95.9", "extra": 1}]))

    assert drifted["value_types"]["rate"] == ["str"]  # type: ignore[index]
    assert drifted["key_sets"] != adapter.fingerprint(raw_of([row()]))["key_sets"]


def test_fingerprint_invalid_json_raises() -> None:
    with pytest.raises(ParseError):
        adapter.fingerprint(raw_of(b"nope"))
