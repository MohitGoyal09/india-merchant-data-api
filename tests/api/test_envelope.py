"""Degraded and stale handling, and tolerance of a missing holiday calendar."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi.testclient import TestClient

from imda.models import IST, Currency, Dataset, FxRate, Source, SourceStatus
from imda.store.repo import Store
from tests.api.conftest import STALE_NOW, MakeClient
from tests.api.helpers import body

FBIL_WINDOW = "/v1/fx/rates?currency=USD&from=2026-09-21&to=2026-09-24&source=fbil"
RBI_WINDOW = "/v1/fx/rates?currency=USD&from=2026-09-21&to=2026-09-24&source=rbi"


def test_fresh_data_is_not_stale_and_not_degraded(make_client: MakeClient) -> None:
    parsed = body(make_client().get(FBIL_WINDOW))

    assert parsed["meta"]["degraded"] is False
    assert parsed["meta"]["warnings"] == []
    assert parsed["provenance"][0]["stale"] is False


def test_fx_is_stale_when_behind_the_last_mumbai_business_day(make_client: MakeClient) -> None:
    client = make_client(now=STALE_NOW)

    fbil = body(client.get(FBIL_WINDOW))
    rbi = body(client.get(RBI_WINDOW))

    assert fbil["provenance"][0]["stale"] is True
    assert any("fbil/fx_reference_rates is stale" in w for w in fbil["meta"]["warnings"])
    assert "expected 2026-09-30" in fbil["meta"]["warnings"][0]
    assert fbil["meta"]["degraded"] is False  # stale is not degraded
    # RBI's newest USD row is 2026-09-30, the last business day before 2026-10-01.
    assert rbi["provenance"][0]["stale"] is False


def test_mibor_staleness_uses_the_same_rule(make_client: MakeClient) -> None:
    client = make_client(now=STALE_NOW)

    parsed = body(client.get("/v1/rates/mibor?from=2026-09-17&to=2026-09-21"))

    assert parsed["provenance"][0]["stale"] is True


def test_holidays_are_never_flagged_stale(make_client: MakeClient) -> None:
    parsed = body(make_client(now=STALE_NOW).get("/v1/holidays?office=mumbai&year=2026"))

    assert parsed["provenance"][0]["stale"] is False


def test_missing_calendar_year_keeps_stale_false_and_adds_a_warning(
    make_client: MakeClient,
) -> None:
    far = dt.datetime(2028, 6, 1, 12, 0, tzinfo=IST)

    parsed = body(make_client(now=far).get(FBIL_WINDOW))

    assert parsed["provenance"][0]["stale"] is False
    assert any("staleness not checked" in w for w in parsed["meta"]["warnings"])


def test_degraded_status_flags_only_the_sources_used(make_client: MakeClient, store: Store) -> None:
    store.set_source_health(Source.FBIL, Dataset.FX, SourceStatus.DEGRADED, error="drift")
    client = make_client()

    fbil = body(client.get(FBIL_WINDOW))
    rbi = body(client.get(RBI_WINDOW))

    assert fbil["meta"]["degraded"] is True
    assert fbil["meta"]["warnings"] == [
        "fbil/fx_reference_rates is degraded; serving the last good data"
    ]
    assert fbil["meta"]["count"] == 4
    assert rbi["meta"]["degraded"] is False


def test_broken_status_is_degraded_too_and_ok_is_not(make_client: MakeClient, store: Store) -> None:
    store.set_source_health(Source.FBIL, Dataset.MIBOR, SourceStatus.BROKEN, error="500")
    store.set_source_health(Source.RBI, Dataset.HOLIDAYS, SourceStatus.OK)
    client = make_client()

    mibor = body(client.get("/v1/rates/mibor?from=2026-09-17&to=2026-09-21"))
    holidays = body(client.get("/v1/holidays?office=mumbai&year=2026"))

    assert mibor["meta"]["degraded"] is True
    assert "broken" in mibor["meta"]["warnings"][0]
    assert holidays["meta"]["degraded"] is False


def test_csv_reports_degraded_in_a_header(make_client: MakeClient, store: Store) -> None:
    store.set_source_health(Source.FBIL, Dataset.FX, SourceStatus.DEGRADED)

    response = make_client().get(FBIL_WINDOW + "&format=csv")

    assert response.headers["X-IMDA-Degraded"] == "true"


def test_stale_csv_header(make_client: MakeClient) -> None:
    response = make_client(now=STALE_NOW).get(FBIL_WINDOW + "&format=csv")

    assert response.headers["X-IMDA-Stale"] == "true"


MONDAY_MORNING = dt.datetime(2026, 10, 5, 9, 0, tzinfo=IST)
# Fri 2026-10-02 is Gandhi Jayanti (a Mumbai holiday) and Sat 10-03 never publishes, so before
# Monday's cutoff the last expected FX publication is Thu 2026-10-01.
MONDAY_FBIL_USD = "/v1/fx/rates?currency=USD&from=2026-09-28&to=2026-10-02&source=fbil"


def _seed_fbil_usd(store: Store, day: dt.date) -> None:
    fetch = store.latest_fetch(Source.FBIL, Dataset.FX)
    assert fetch is not None
    row = FxRate(currency=Currency.USD, date=day, rate=Decimal("88.5"), unit=1, source=Source.FBIL)
    store.upsert_fx_rates([row], fetch.fetch_id)


def _health_stale(client: TestClient, source: str) -> bool:
    sources = body(client.get("/v1/sources/health"))["data"]["sources"]
    (item,) = [s for s in sources if (s["source"], s["dataset"]) == (source, "fx_reference_rates")]
    return bool(item["freshness"]["stale"])


def test_a_saturday_is_never_expected_so_latest_thursday_is_fresh_on_monday(
    make_client: MakeClient, store: Store
) -> None:
    _seed_fbil_usd(store, dt.date(2026, 10, 1))
    client = make_client(now=MONDAY_MORNING)

    parsed = body(client.get(MONDAY_FBIL_USD))

    assert parsed["provenance"][0]["stale"] is False
    assert _health_stale(client, "fbil") is False


def test_envelope_and_sources_health_agree_when_stale(
    make_client: MakeClient, store: Store
) -> None:
    _seed_fbil_usd(store, dt.date(2026, 9, 30))
    client = make_client(now=MONDAY_MORNING)

    parsed = body(client.get(MONDAY_FBIL_USD))

    assert parsed["provenance"][0]["stale"] is True
    assert _health_stale(client, "fbil") is True
    assert "expected 2026-10-01" in parsed["meta"]["warnings"][0]
