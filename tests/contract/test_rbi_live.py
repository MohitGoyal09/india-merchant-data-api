"""Live RBI contract checks. Opt in with ``pytest -m live``. At most 4 requests, 2 s apart."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from imda.config import Settings
from imda.http.client import PoliteClient
from imda.models import Currency, HolidayKind
from imda.sources.base import DateRangeQuery, HolidayQuery, RawPayload, UpstreamRequest
from imda.sources.rbi.fx import RbiFxAdapter
from imda.sources.rbi.holidays import RbiHolidayAdapter
from imda.sources.rbi.offices import parse_offices

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "rbi"
MUMBAI_MARCH_2026 = {3, 19, 21, 26, 31}


def recorded(name: str) -> RawPayload:
    meta = json.loads((FIXTURES / f"{name}.meta.json").read_text(encoding="utf-8"))
    body = (FIXTURES / f"{name}.html").read_bytes()
    return RawPayload(
        request=UpstreamRequest(method=meta["method"], url=meta["url"], form=meta["form"] or None),
        status_code=200,
        body=body,
        content_type="text/html",
        fetched_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
        sha256=hashlib.sha256(body).hexdigest(),
        duration_ms=0,
    )


@pytest.fixture(scope="module")
def client() -> Iterator[PoliteClient]:
    with PoliteClient(Settings(_env_file=None)) as polite:
        yield polite


def test_live_holidays_matrix_matches_parser_and_baseline(client: PoliteClient) -> None:
    adapter = RbiHolidayAdapter()

    [raw] = adapter.fetch(client, HolidayQuery(year=2026, month=3))
    holidays = adapter.parse(raw)

    assert raw.status_code == 200
    assert len(parse_offices(raw.text())) == 34
    mumbai = {h.date.day for h in holidays if h.office_slug == "mumbai"}
    assert mumbai == MUMBAI_MARCH_2026
    assert {h.kind for h in holidays} <= set(HolidayKind)
    assert adapter.fingerprint(raw) == adapter.fingerprint(recorded("holidays_all_2026_03"))


def test_live_fx_matches_parser_and_baseline(client: PoliteClient) -> None:
    adapter = RbiFxAdapter()

    [raw] = adapter.fetch(client, DateRangeQuery(dt.date(2026, 9, 1), dt.date(2026, 9, 30)))
    rates = adapter.parse(raw)

    assert raw.status_code == 200
    assert {r.currency for r in rates} == set(Currency)
    assert all(dt.date(2026, 9, 1) <= r.date <= dt.date(2026, 9, 30) for r in rates)
    assert adapter.fingerprint(raw) == adapter.fingerprint(recorded("fx_2026_09"))
