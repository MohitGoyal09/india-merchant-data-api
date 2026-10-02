"""Live FBIL contract checks. Opt in with ``pytest -m live``. At most 3 requests."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest

from imda.config import Settings
from imda.http.client import PoliteClient
from imda.models import Currency
from imda.sources.base import DateRangeQuery
from imda.sources.fbil.fx import FbilFxAdapter
from imda.sources.fbil.mibor import FbilMiborAdapter

pytestmark = pytest.mark.live

WINDOW = DateRangeQuery(dt.date(2026, 9, 1), dt.date(2026, 9, 30))


@pytest.fixture(scope="module")
def client() -> Iterator[PoliteClient]:
    with PoliteClient(Settings(_env_file=None)) as polite:
        yield polite


def test_live_fx_matches_parser(client: PoliteClient) -> None:
    adapter = FbilFxAdapter()

    [raw] = adapter.fetch(client, WINDOW)
    report = adapter.parse_with_report(raw)

    assert raw.status_code == 200
    assert {r.currency for r in report.rates} == set(Currency)
    assert all(r.date >= WINDOW.start for r in report.rates)
    assert adapter.fingerprint(raw)["row_count"] >= len(report.rates)


def test_live_mibor_matches_parser(client: PoliteClient) -> None:
    adapter = FbilMiborAdapter()

    [raw] = adapter.fetch(client, WINDOW)
    rates = adapter.parse(raw)

    assert rates
    assert {r.tenor for r in rates} >= {"O/N"}
