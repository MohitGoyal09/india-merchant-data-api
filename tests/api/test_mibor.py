"""``GET /v1/rates/mibor``."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi.testclient import TestClient

from imda.models import Dataset, MiborRate, Source
from imda.store.repo import Store
from tests.api.conftest import FETCHED_AT, _log_fetch, _payload
from tests.api.helpers import assert_envelope, assert_error, body

URL = "/v1/rates/mibor"


def test_friday_publishes_3d_which_keeps_its_tenor_and_spans_the_weekend(
    client: TestClient,
) -> None:
    parsed = body(client.get(f"{URL}?from=2026-09-17&to=2026-09-21"))

    assert_envelope(parsed)
    assert [(r["date"], r["tenor"], r["spans_weekend"]) for r in parsed["data"]] == [
        ("2026-09-17", "O/N", False),
        ("2026-09-18", "3D", True),
        ("2026-09-21", "O/N", False),
    ]
    friday = parsed["data"][1]
    assert friday["rate"] == "5.1"
    assert friday["source"] == "fbil"
    assert friday["published_at"] == "2026-09-18T12:45:00+05:30"
    (prov,) = parsed["provenance"]
    assert (prov["source"], prov["dataset"], prov["stale"]) == ("fbil", "mibor_overnight", False)


def test_overnight_only_filters_other_tenors(client: TestClient, store: Store) -> None:
    fetch = _log_fetch(store, Source.FBIL, Dataset.MIBOR, _payload("fbil", "mibor_2026_09", "json"))
    store.upsert_mibor(
        [MiborRate(date=dt.date(2026, 9, 17), tenor="1M", rate=Decimal("6.25"))], fetch
    )

    only = body(client.get(f"{URL}?from=2026-09-17&to=2026-09-17"))
    everything = body(client.get(f"{URL}?from=2026-09-17&to=2026-09-17&overnight_only=false"))

    assert [r["tenor"] for r in only["data"]] == ["O/N"]
    assert [r["tenor"] for r in everything["data"]] == ["1M", "O/N"]
    assert FETCHED_AT  # fixture timestamp is shared with the seed


def test_empty_range_still_has_provenance(client: TestClient) -> None:
    parsed = body(client.get(f"{URL}?from=2020-01-01&to=2020-01-31"))

    assert parsed["data"] == []
    (entry,) = parsed["provenance"]
    assert (entry["source"], entry["dataset"]) == ("fbil", "mibor_overnight")
    assert entry["source_url"].startswith("https://")
    assert entry["fetched_at"] is not None


def test_validation(client: TestClient) -> None:
    assert_error(client.get(f"{URL}?from=2026-09-21&to=2026-09-17"), 422, "VALIDATION_ERROR")
    assert_error(client.get(f"{URL}?from=2000-01-01&to=2026-09-17"), 422, "RANGE_TOO_LARGE")
    assert_error(client.get(f"{URL}?from=17-09-2026&to=2026-09-18"), 422, "INVALID_REQUEST")
    assert_error(client.get(f"{URL}?to=2026-09-18"), 422, "INVALID_REQUEST")
    assert_error(
        client.get(f"{URL}?from=2026-09-17&to=2026-09-18&overnight_only=maybe"),
        422,
        "INVALID_REQUEST",
    )
