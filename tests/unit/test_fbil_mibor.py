from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from imda.models import IST, Dataset, Source
from imda.sources.base import DateRangeQuery, ParseError, RawPayload, UpstreamRequest
from imda.sources.fbil.mibor import FbilMiborAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "fbil"
URL = "https://www.fbil.org.in/wasdm/ovnmibor/fetchfiltered"


def raw_of(body: object) -> RawPayload:
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    return RawPayload(
        request=UpstreamRequest(method="GET", url=URL),
        status_code=200,
        body=payload,
        content_type="application/json",
        fetched_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
        sha256="0" * 64,
        duration_ms=1,
    )


def row(tenor: str = "O/N", day: str = "2026-09-24", rate: object = 5.13) -> dict[str, object]:
    return {
        "processRunDate": f"{day} 00:00:00",
        "tenor": tenor,
        "displayTime": f"{day} 12:45:00",
        "rate": rate,
    }


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[UpstreamRequest] = []

    def send(self, request: UpstreamRequest) -> RawPayload:
        self.requests.append(request)
        return raw_of([])


adapter = FbilMiborAdapter()


def test_identity() -> None:
    assert adapter.source is Source.FBIL
    assert adapter.dataset is Dataset.MIBOR


def test_fetch_uses_mibor_endpoint_and_chunks() -> None:
    client = FakeClient()

    adapter.fetch(client, DateRangeQuery(dt.date(2015, 7, 22), dt.date(2026, 9, 24)))

    assert len(client.requests) == 12
    assert all(r.url == URL for r in client.requests)
    first = client.requests[0]
    assert dict(first.params) == {
        "fromDate": "2015-07-22",
        "toDate": "2016-07-21",
        "authenticated": "false",
    }
    assert client.requests[-1].params["toDate"] == "2026-09-24"


def test_parse_recorded_september_2026() -> None:
    raw = raw_of((FIXTURES / "mibor_2026_09.json").read_bytes())

    rates = adapter.parse(raw)

    assert len(rates) == 17
    newest = rates[0]
    assert newest.date == dt.date(2026, 9, 24)
    assert newest.tenor == "O/N"
    assert newest.rate == Decimal("5.13")
    assert newest.source is Source.FBIL
    assert newest.published_at == dt.datetime(2026, 9, 24, 12, 45, tzinfo=IST)
    assert {r.tenor for r in rates} == {"O/N", "3D"}  # Fridays carry a 3-day tenor


def test_float_goes_through_str() -> None:
    [rate] = adapter.parse(raw_of([row(rate=0.1)]))

    assert rate.rate == Decimal("0.1")


def test_identical_duplicates_collapse_but_conflicts_raise() -> None:
    assert len(adapter.parse(raw_of([row(), row()]))) == 1
    assert len(adapter.parse(raw_of([row("O/N"), row("3D")]))) == 2
    with pytest.raises(ParseError, match="conflicting duplicate"):
        adapter.parse(raw_of([row(rate=5.1), row(rate=5.2)]))


def test_negative_rate_is_allowed_by_the_model() -> None:
    [rate] = adapter.parse(raw_of([row(rate=-0.5)]))

    assert rate.rate == Decimal("-0.5")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"garbage", "not valid JSON"),
        ({"x": 1}, "expected a JSON list"),
        ([[1]], "not an object"),
    ],
)
def test_bad_shapes_raise(body: object, reason: str) -> None:
    with pytest.raises(ParseError, match=reason) as exc:
        adapter.parse(raw_of(body))

    assert exc.value.dataset is Dataset.MIBOR


@pytest.mark.parametrize("missing", ["processRunDate", "tenor", "displayTime", "rate"])
def test_missing_key_raises(missing: str) -> None:
    bad = row()
    del bad[missing]

    with pytest.raises(ParseError, match=missing):
        adapter.parse(raw_of([bad]))


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"tenor": ""}, "tenor"),
        ({"tenor": 3}, "tenor"),
        ({"rate": "x"}, "not a number"),
        ({"processRunDate": "2026-09-24"}, "processRunDate is not"),
    ],
)
def test_bad_values_raise(patch: dict[str, object], reason: str) -> None:
    with pytest.raises(ParseError, match=reason):
        adapter.parse(raw_of([{**row(), **patch}]))


def test_fingerprint_is_structural() -> None:
    raw = raw_of((FIXTURES / "mibor_2026_09.json").read_bytes())

    fp = adapter.fingerprint(raw)

    assert fp["row_count"] == 17
    assert fp["key_sets"] == [["displayTime", "processRunDate", "rate", "tenor"]]
    assert fp["value_types"]["rate"] == ["float"]  # type: ignore[index]
    assert fp["label_pattern_count"] == 2  # "O/N" and "ND"
    assert "5.13" not in json.dumps(fp)
