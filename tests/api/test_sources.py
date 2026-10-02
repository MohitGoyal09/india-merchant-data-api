"""``GET /v1/sources/health``."""

from __future__ import annotations

from fastapi.testclient import TestClient

from imda.health.drift import DriftReport
from imda.models import Dataset, Source, SourceStatus
from imda.store.repo import Store
from tests.api.conftest import STALE_NOW, MakeClient
from tests.api.helpers import assert_envelope, body

URL = "/v1/sources/health"


def by_key(parsed: dict) -> dict[str, dict]:  # type: ignore[type-arg]
    return {f"{s['source']}/{s['dataset']}": s for s in parsed["data"]["sources"]}


def test_empty_health_still_reports_freshness_and_unknown_status(client: TestClient) -> None:
    parsed = body(client.get(URL))

    assert_envelope(parsed)
    assert parsed["data"]["status"] == "unknown"
    sources = by_key(parsed)
    assert set(sources) == {
        "rbi/fx_reference_rates",
        "fbil/fx_reference_rates",
        "fbil/mibor_overnight",
    }
    fbil = sources["fbil/fx_reference_rates"]
    assert fbil["status"] == "unknown"
    assert fbil["checked_at"] is None
    assert fbil["drift"] is None
    assert fbil["freshness"] == {
        "latest_date": "2026-09-24",
        "expected_date": "2026-09-25",
        "lag_business_days": 1,
        "stale": True,
        "calendar_incomplete": False,
    }
    assert parsed["data"]["last_runs"] == []


def test_row_carries_health_fields_drift_summary_and_freshness(
    client: TestClient, store: Store
) -> None:
    drift = DriftReport(
        drifted=True, added_keys=("extra",), changed={"unit": ("INR", "USD")}
    ).as_dict()
    store.set_source_health(Source.FBIL, Dataset.FX, SourceStatus.OK)
    store.set_source_health(
        Source.FBIL, Dataset.FX, SourceStatus.DEGRADED, error="fingerprint drift", drift=drift
    )

    parsed = body(client.get(URL))

    row = by_key(parsed)["fbil/fx_reference_rates"]
    assert row["status"] == "degraded"
    assert row["last_error"] == "fingerprint drift"
    assert row["checked_at"]
    assert row["last_success_at"]
    assert row["last_error_at"]
    assert row["drift"]["drifted"] is True
    assert row["drift"]["added_keys"] == ["extra"]
    assert row["drift"]["changed_keys"] == ["unit"]
    assert row["drift"]["summary"].startswith("fingerprint drift: added extra")
    assert row["freshness"]["latest_date"] == "2026-09-24"
    assert parsed["meta"]["degraded"] is True
    assert any("degraded" in w for w in parsed["meta"]["warnings"])


def test_overall_status_is_the_worst(client: TestClient, store: Store) -> None:
    store.set_source_health(Source.RBI, Dataset.HOLIDAYS, SourceStatus.OK)
    store.set_source_health(Source.RBI, Dataset.FX, SourceStatus.DEGRADED, error="x")
    store.set_source_health(Source.FBIL, Dataset.MIBOR, SourceStatus.BROKEN, error="parse")

    parsed = body(client.get(URL))

    assert parsed["data"]["status"] == "broken"
    sources = by_key(parsed)
    assert sources["rbi/holidays"]["freshness"] is None  # no freshness for reference data
    assert sources["rbi/holidays"]["status"] == "ok"
    assert parsed["meta"]["count"] == len(parsed["data"]["sources"]) == 4


def test_all_ok_is_ok(client: TestClient, store: Store) -> None:
    for source, dataset in (
        (Source.RBI, Dataset.FX),
        (Source.FBIL, Dataset.FX),
        (Source.FBIL, Dataset.MIBOR),
    ):
        store.set_source_health(source, dataset, SourceStatus.OK)

    parsed = body(client.get(URL))

    assert parsed["data"]["status"] == "ok"
    assert parsed["meta"]["degraded"] is False
    assert {s["last_error"] for s in parsed["data"]["sources"]} == {None}


def test_stale_data_is_flagged_with_a_warning(make_client: MakeClient) -> None:
    parsed = body(make_client(now=STALE_NOW).get(URL))

    fbil = by_key(parsed)["fbil/fx_reference_rates"]
    assert fbil["freshness"]["stale"] is True
    assert fbil["freshness"]["expected_date"] == "2026-10-01"
    assert fbil["freshness"]["lag_business_days"] == 5
    assert any("fbil/fx_reference_rates is stale" in w for w in parsed["meta"]["warnings"])


def test_last_runs_are_the_latest_five(client: TestClient, store: Store) -> None:
    for _ in range(6):
        run_id = store.start_run("refresh")
        store.finish_run(run_id, "ok", {})
    store.start_run("canary")

    runs = body(client.get(URL))["data"]["last_runs"]

    assert len(runs) == 5
    assert runs[0]["kind"] == "canary"
    assert runs[0]["status"] == "running"
    assert runs[0]["finished_at"] is None
    assert set(runs[1]) == {"run_id", "kind", "status", "started_at", "finished_at"}
    assert runs[1]["status"] == "ok"
    assert runs[1]["finished_at"]


def test_public_no_token_needed(client: TestClient) -> None:
    assert client.get(URL).status_code == 200


def test_malformed_stored_drift_does_not_break_the_endpoint(
    client: TestClient, store: Store
) -> None:
    store.set_source_health(
        Source.RBI,
        Dataset.FX,
        SourceStatus.DEGRADED,
        error="x",
        drift={
            "drifted": True,
            "added_keys": "oops",
            "changed": {"k": "not-a-dict", "j": {"old": 1}},
        },
    )

    row = by_key(body(client.get(URL)))["rbi/fx_reference_rates"]

    assert row["drift"]["drifted"] is True
    assert row["drift"]["added_keys"] == []
    assert row["drift"]["changed_keys"] == ["j"]
