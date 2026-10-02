"""Root, liveness, request ids, access log, generic errors and the calendar cache."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from imda import __version__
from imda.api.app import create_app
from imda.config import Settings
from imda.store.repo import Store
from tests.api.conftest import FRESH_NOW, MakeClient
from tests.api.helpers import assert_error, body


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def access_lines() -> Iterator[list[str]]:
    handler = _ListHandler()
    logger = logging.getLogger("imda.api.access")
    logger.addHandler(handler)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)


def test_root_lists_links(client: TestClient) -> None:
    parsed = body(client.get("/"))

    assert parsed["name"] == "India Merchant Data API"
    assert parsed["version"] == __version__
    assert parsed["endpoints"]["offices"] == "/v1/offices"


def test_healthz_does_not_touch_the_database(make_client: MakeClient, settings: Settings) -> None:
    client = make_client()
    settings.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        settings.db_path.with_name(settings.db_path.name + suffix).unlink(missing_ok=True)

    assert body(client.get("/healthz")) == {"status": "ok", "version": __version__}
    assert not settings.db_path.exists()


def test_generated_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz")

    assert re.fullmatch(r"[A-Za-z0-9-]{1,64}", response.headers["X-Request-ID"])


def test_valid_incoming_request_id_is_kept(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "trace-123-abc"})

    assert response.headers["X-Request-ID"] == "trace-123-abc"


@pytest.mark.parametrize("bad", ["has space", "semi;colon", "x" * 65, "new\tline", "é"])
def test_invalid_incoming_request_id_is_replaced(client: TestClient, bad: str) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": bad.encode("latin-1")})

    echoed = response.headers["X-Request-ID"]
    assert echoed != bad
    assert re.fullmatch(r"[A-Za-z0-9-]{1,64}", echoed)


def test_error_body_carries_the_request_id(client: TestClient) -> None:
    response = client.get("/v1/offices/none", headers={"X-Request-ID": "rid-9"})

    parsed = assert_error(response, 404, "NOT_FOUND")
    assert parsed["request_id"] == "rid-9"
    assert response.headers["X-Request-ID"] == "rid-9"


def test_access_log_line_is_structured_and_has_no_query_string(
    client: TestClient, access_lines: list[str]
) -> None:
    client.get("/v1/offices?token=sekret", headers={"X-Request-ID": "log-1", "Authorization": "x"})

    record = json.loads(access_lines[-1])
    assert record["method"] == "GET"
    assert record["path"] == "/v1/offices"
    assert record["status"] == 200
    assert record["request_id"] == "log-1"
    assert record["duration_ms"] >= 0
    assert record["route"] == "/v1/offices"
    assert set(record) == {
        "event",
        "method",
        "path",
        "route",
        "status",
        "duration_ms",
        "request_id",
    }
    assert "sekret" not in access_lines[-1]


def test_unknown_route_is_a_json_404(client: TestClient) -> None:
    assert_error(client.get("/v1/nothing"), 404, "NOT_FOUND")


def test_wrong_method_is_a_json_405(client: TestClient) -> None:
    assert_error(client.post("/v1/offices"), 405, "METHOD_NOT_ALLOWED")


def test_unhandled_exception_is_a_generic_500_and_is_logged(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client()

    def boom() -> None:
        raise RuntimeError("secret internal detail")

    client.app.add_api_route("/boom", boom)  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR, logger="imda.api"):
        response = client.get("/boom", headers={"X-Request-ID": "boom-1"})

    parsed = assert_error(response, 500, "INTERNAL_ERROR")
    assert parsed["error"]["message"] == "Internal server error"
    assert "secret internal detail" not in response.text
    assert parsed["request_id"] == "boom-1"
    assert any("boom-1" in r.getMessage() and r.exc_info for r in caplog.records)


def test_openapi_has_title_version_tags_and_examples(client: TestClient) -> None:
    spec = body(client.get("/openapi.json"))

    assert spec["info"]["title"] == "India Merchant Data API"
    assert spec["info"]["version"] == __version__
    assert {t["name"] for t in spec["tags"]} >= {"offices", "holidays", "calendar", "fx"}
    ok = spec["paths"]["/v1/fx/rates/as-of"]["get"]["responses"]["200"]
    assert "example" in ok["content"]["application/json"]


def test_calendar_snapshot_is_cached_for_60_seconds(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings, now=lambda: FRESH_NOW)
    clock = [1000.0]
    app.state.snapshots.monotonic = lambda: clock[0]
    loads = []
    original = Store.holidays

    def counting(self: Store, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        loads.append(1)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Store, "holidays", counting)
    client = TestClient(app)

    for _ in range(3):
        body(client.get("/v1/calendar/business-day?date=2026-09-14&office=mumbai"))
    assert len(loads) == 1

    clock[0] += 59
    body(client.get("/v1/calendar/business-day?date=2026-09-14&office=mumbai"))
    assert len(loads) == 1

    clock[0] += 2
    body(client.get("/v1/calendar/business-day?date=2026-09-14&office=mumbai"))
    assert len(loads) == 2


def test_default_clock_is_timezone_aware_ist(settings: Settings) -> None:
    app = create_app(settings)

    now = app.state.now()

    assert now.utcoffset() is not None
    assert now.utcoffset().total_seconds() == 5.5 * 3600


def test_each_request_closes_its_store(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    original = Store.close

    def tracking(self: Store) -> None:
        closed.append(1)
        original(self)

    monkeypatch.setattr(Store, "close", tracking)

    client.get("/v1/offices")
    client.get("/v1/holidays?office=nowhere&year=2026")  # an error path closes it too

    assert len(closed) == 2
