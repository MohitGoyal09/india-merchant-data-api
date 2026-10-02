"""GET /readyz: readiness (migrated DB, data loaded, no broken source) versus /healthz liveness."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from imda.config import Settings
from imda.models import Dataset, Source, SourceStatus
from imda.observability import route_template
from imda.store.repo import Store
from tests.api.conftest import MakeClient
from tests.api.helpers import body

CHECKS = {"database", "fx_rates", "holiday_years", "sources"}


def _assert_no_leaks(text: str, db_path: Path) -> None:
    assert str(db_path) not in text
    assert "Traceback" not in text
    assert "sqlite3" not in text


def test_ready_on_the_seeded_database(client: TestClient) -> None:
    response = client.get("/readyz")

    parsed = body(response)
    assert response.status_code == 200
    assert parsed["status"] == "ready"
    assert set(parsed["checks"]) == CHECKS
    for check in parsed["checks"].values():
        assert check["ok"] is True
        assert check["reason"]


def test_readyz_needs_no_auth_and_is_fast(client: TestClient) -> None:
    client.get("/readyz")  # warm up imports and the connection path

    started = time.perf_counter()
    response = client.get("/readyz")

    assert response.status_code == 200
    assert time.perf_counter() - started < 0.05


def test_not_ready_on_an_empty_migrated_database(make_client: MakeClient, tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite3"
    Store.open(empty).close()
    client = make_client(custom=Settings(db_path=empty, _env_file=None))  # type: ignore[call-arg]

    response = client.get("/readyz")

    parsed = body(response, 503)
    assert response.status_code == 503
    assert parsed["status"] == "not_ready"
    assert parsed["checks"]["database"]["ok"] is True
    assert parsed["checks"]["fx_rates"]["ok"] is False
    assert parsed["checks"]["holiday_years"]["ok"] is False
    assert parsed["checks"]["sources"]["ok"] is True
    _assert_no_leaks(response.text, empty)


def test_not_ready_when_a_source_is_broken(client: TestClient, store: Store, db_path: Path) -> None:
    store.set_source_health(Source.RBI, Dataset.FX, SourceStatus.BROKEN, error="boom /secret/path")

    response = client.get("/readyz")

    parsed = body(response, 503)
    assert response.status_code == 503
    assert parsed["status"] == "not_ready"
    assert parsed["checks"]["sources"]["ok"] is False
    assert "rbi/fx" in parsed["checks"]["sources"]["reason"]
    assert parsed["checks"]["fx_rates"]["ok"] is True
    assert "secret" not in response.text
    _assert_no_leaks(response.text, db_path)


def test_degraded_source_is_still_ready(client: TestClient, store: Store) -> None:
    store.set_source_health(Source.FBIL, Dataset.MIBOR, SourceStatus.DEGRADED, error="slow")

    assert client.get("/readyz").status_code == 200


def test_503_when_the_database_file_is_missing(make_client: MakeClient, db_path: Path) -> None:
    client = make_client()
    db_path.unlink()
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

    response = client.get("/readyz")

    parsed = body(response, 503)
    assert response.status_code == 503
    assert parsed["status"] == "not_ready"
    assert parsed["checks"]["database"]["ok"] is False
    assert "missing" in parsed["checks"]["database"]["reason"]
    assert set(parsed["checks"]) == CHECKS
    assert not db_path.exists()
    _assert_no_leaks(response.text, db_path)


def test_503_when_the_database_is_not_a_database(make_client: MakeClient, db_path: Path) -> None:
    client = make_client()
    db_path.write_bytes(b"this is not sqlite" * 100)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert body(response, 503)["checks"]["database"]["ok"] is False
    _assert_no_leaks(response.text, db_path)


def test_healthz_stays_pure_liveness(make_client: MakeClient, db_path: Path) -> None:
    client = make_client()
    db_path.unlink()

    assert client.get("/healthz").status_code == 200


def test_route_template_names_the_matched_route_not_the_path() -> None:
    app = FastAPI()
    seen: list[str | None] = []

    @app.middleware("http")
    async def record(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        seen.append(route_template(request))
        return response

    @app.get("/v1/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    client = TestClient(app)
    client.get("/v1/items/abc123")
    client.get("/nothing-here")

    assert seen == ["/v1/items/{item_id}", None]
