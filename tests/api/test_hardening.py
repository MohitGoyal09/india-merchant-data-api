"""Startup, error typing, date bounds, HTTP hardening and exact-decimal behaviour."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from imda.api.app import BodyLimitMiddleware, create_app
from imda.api.deps import SnapshotCache
from imda.api.routes.fx import encode_cursor
from imda.config import Settings
from imda.errors import InvalidInput
from imda.models import FxRate
from imda.store.repo import Store
from tests.api.conftest import FRESH_NOW, MakeClient
from tests.api.helpers import assert_error, body

RATES = "/v1/fx/rates"
QUOTE = "/v1/invoice/quote"
GOOD_QUOTE = {
    "amount": "10.00",
    "currency": "USD",
    "invoice_date": "2026-09-24",
    "office": "mumbai",
}


# --- 1. startup migrate, read-only requests ---------------------------------------------------


def test_startup_creates_and_migrates_the_store(tmp_path: Path) -> None:
    path = tmp_path / "fresh" / "imda.sqlite3"
    path.parent.mkdir()
    app = create_app(Settings(db_path=path, _env_file=None), now=lambda: FRESH_NOW)

    with TestClient(app) as client:
        assert path.exists()
        assert body(client.get("/v1/offices"))["data"] == []


def test_missing_store_is_503_store_unavailable_and_is_not_created(
    tmp_path: Path, make_client: MakeClient
) -> None:
    path = tmp_path / "absent.sqlite3"
    client = make_client(custom=Settings(db_path=path, _env_file=None))

    parsed = assert_error(client.get("/v1/offices"), 503, "STORE_UNAVAILABLE")

    assert "imda backfill" in parsed["error"]["message"]
    assert not path.exists()


def test_store_is_closed_when_building_the_context_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Store] = []
    original = Store.open.__func__  # type: ignore[attr-defined]

    def tracking(cls: type[Store], *args: Any, **kwargs: Any) -> Store:
        store: Store = original(cls, *args, **kwargs)
        opened.append(store)
        return store

    def broken(self: SnapshotCache, store: Store) -> None:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(Store, "open", classmethod(tracking))
    monkeypatch.setattr(SnapshotCache, "get", broken)

    assert_error(client.get("/v1/offices"), 500, "INTERNAL_ERROR")

    assert len(opened) == 1
    with pytest.raises(Exception, match="closed"):
        opened[0].connection.execute("SELECT 1")


def test_requests_open_the_store_read_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs_seen: list[dict[str, Any]] = []
    original = Store.open.__func__  # type: ignore[attr-defined]

    def tracking(cls: type[Store], *args: Any, **kwargs: Any) -> Store:
        kwargs_seen.append(kwargs)
        opened: Store = original(cls, *args, **kwargs)
        return opened

    monkeypatch.setattr(Store, "open", classmethod(tracking))

    body(client.get("/v1/offices"))

    assert kwargs_seen == [{"read_only": True}]


# --- 2. input errors vs internal errors -------------------------------------------------------


class _Strict(BaseModel):
    n: int


@pytest.fixture
def boom_client(make_client: MakeClient) -> TestClient:
    client = make_client()

    def plain() -> None:
        raise ValueError("secret internal detail")

    def invalid() -> None:
        raise InvalidInput("bad thing the client sent")

    def pydantic() -> None:
        try:
            _Strict(n="x")
        except ValidationError:
            raise

    for path, fn in (("/plain", plain), ("/invalid", invalid), ("/pydantic", pydantic)):
        client.app.add_api_route(path, fn)  # type: ignore[attr-defined]
    return client


def test_invalid_input_is_422_validation_error(boom_client: TestClient) -> None:
    parsed = assert_error(boom_client.get("/invalid"), 422, "VALIDATION_ERROR")

    assert parsed["error"]["message"] == "bad thing the client sent"


@pytest.mark.parametrize("path", ["/plain", "/pydantic"])
def test_plain_value_error_is_a_generic_500_with_a_log(
    boom_client: TestClient, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.ERROR, logger="imda.api"):
        response = boom_client.get(path, headers={"X-Request-ID": "rid-500"})

    parsed = assert_error(response, 500, "INTERNAL_ERROR")
    assert parsed["error"]["message"] == "Internal server error"
    assert "secret internal detail" not in response.text
    assert any("rid-500" in r.getMessage() for r in caplog.records)


# --- 3. date edges ----------------------------------------------------------------------------


@pytest.mark.parametrize("day", ["1999-12-31", "2101-01-01", "0001-01-01", "9999-12-31"])
def test_dates_outside_2000_to_2100_are_invalid_requests(client: TestClient, day: str) -> None:
    assert_error(client.get(f"/v1/fx/rates/as-of?currency=USD&date={day}"), 422, "INVALID_REQUEST")
    assert_error(
        client.get(f"/v1/calendar/business-day?date={day}&office=mumbai"), 422, "INVALID_REQUEST"
    )
    assert_error(
        client.get(f"{RATES}?currency=USD&from={day}&to=2026-09-24"), 422, "INVALID_REQUEST"
    )


def test_the_boundary_dates_themselves_are_accepted(client: TestClient) -> None:
    assert_error(
        client.get("/v1/fx/rates/as-of?currency=USD&date=2000-01-01"), 404, "RATE_NOT_FOUND"
    )
    assert_error(
        client.get("/v1/fx/rates/as-of?currency=USD&date=2100-12-31"), 404, "RATE_NOT_FOUND"
    )


def test_invoice_body_date_is_bounded_too(client: TestClient) -> None:
    payload = {**GOOD_QUOTE, "invoice_date": "1999-01-01"}

    assert_error(client.post(QUOTE, json=payload), 422, "INVALID_REQUEST")


@pytest.mark.parametrize(
    "cursor",
    [encode_cursor(dt.date.max), encode_cursor(dt.date(1999, 1, 1)), "!!!", "MDAwMC0wMC0wMA"],
)
def test_bad_or_overflowing_cursor_is_rejected_not_a_500(client: TestClient, cursor: str) -> None:
    response = client.get(f"{RATES}?currency=USD&from=2021-01-01&to=2021-02-01&cursor={cursor}")

    parsed = assert_error(response, 422, "INVALID_REQUEST")
    (error,) = parsed["error"]["details"]["errors"]
    assert (error["field"], error["in"], error["message"]) == ("cursor", "query", "invalid cursor")


# --- 5. exact is plain decimal text -----------------------------------------------------------


def test_exact_is_plain_decimal_text_for_tiny_results(client: TestClient) -> None:
    parsed = body(
        client.get("/v1/fx/convert?amount=0.01&from=IDR&to=USD&date=2026-09-24&source=fbil")
    )

    exact = parsed["data"]["exact"]
    assert isinstance(exact, str)
    assert "E" not in exact.upper()
    assert exact.startswith("0.0000")
    assert parsed["data"]["result"] == "0.00"


# --- 9. HTTP hardening ------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/v1/offices", "/v1/nothing", "/v1/fx/rates"])
def test_every_response_carries_the_security_headers(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_oversized_content_length_is_413_before_the_body_is_read(
    settings: Settings, make_client: MakeClient
) -> None:
    client = make_client(custom=settings.model_copy(update={"max_request_body_bytes": 2048}))
    big = {**GOOD_QUOTE, "pad": "x" * 4096}

    response = client.post(QUOTE, json=big)

    parsed = assert_error(response, 413, "PAYLOAD_TOO_LARGE")
    assert parsed["error"]["details"] == {"max_bytes": 2048}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Request-ID"] == parsed["request_id"]


def test_oversized_chunked_body_is_413(settings: Settings, make_client: MakeClient) -> None:
    client = make_client(custom=settings.model_copy(update={"max_request_body_bytes": 2048}))

    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield b"x" * 512

    response = client.post(QUOTE, content=chunks(), headers={"Content-Type": "application/json"})

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")


def test_small_chunked_body_still_works(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        raw = json.dumps(GOOD_QUOTE).encode()
        yield raw[:20]
        yield raw[20:]

    response = client.post(QUOTE, content=chunks(), headers={"Content-Type": "application/json"})

    assert body(response)["data"]["conversion"]["from"] == "USD"


def test_body_exactly_at_the_limit_is_accepted(settings: Settings, make_client: MakeClient) -> None:
    raw = json.dumps(GOOD_QUOTE).encode()
    limit = max(len(raw), 1024)
    client = make_client(custom=settings.model_copy(update={"max_request_body_bytes": limit}))
    padded = raw + b" " * (limit - len(raw))

    response = client.post(QUOTE, content=padded, headers={"Content-Type": "application/json"})

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_can_be_switched_off(settings: Settings, make_client: MakeClient, path: str) -> None:
    on = make_client()
    off = make_client(custom=settings.model_copy(update={"enable_docs": False}))

    assert on.get(path).status_code == 200
    assert_error(off.get(path), 404, "NOT_FOUND")


def test_root_does_not_advertise_docs_when_they_are_off(
    settings: Settings, make_client: MakeClient
) -> None:
    off = make_client(custom=settings.model_copy(update={"enable_docs": False}))

    parsed = body(off.get("/"))

    assert "docs" not in parsed
    assert "openapi" not in parsed


# --- 10. one rates query for stats ------------------------------------------------------------


def test_stats_queries_rates_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    original = Store.fx_rates

    def counting(self: Store, *args: Any, **kwargs: Any) -> list[FxRate]:
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Store, "fx_rates", counting)

    body(client.get("/v1/fx/stats?currency=USD&from=2026-09-01&to=2026-09-24&period=week"))

    assert len(calls) == 1


# --- 6. provenance on empty results (fx) ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"{RATES}?currency=USD&from=2020-01-01&to=2020-01-05",
        "/v1/fx/stats?currency=USD&from=2020-01-01&to=2020-01-05",
    ],
)
def test_empty_fx_results_still_carry_provenance(client: TestClient, url: str) -> None:
    parsed = body(client.get(url))

    assert parsed["data"] == []
    assert {p["source"] for p in parsed["provenance"]} == {"rbi", "fbil"}
    for entry in parsed["provenance"]:
        assert entry["dataset"] == "fx_reference_rates"
        assert entry["source_url"].startswith("https://")
        assert entry["fetched_at"] is not None


def test_empty_forced_source_fx_result_names_that_source(client: TestClient) -> None:
    parsed = body(client.get(f"{RATES}?currency=USD&from=2020-01-01&to=2020-01-05&source=rbi"))

    assert [p["source"] for p in parsed["provenance"]] == ["rbi"]


def test_chunked_client_that_disconnects_is_passed_through_not_rejected() -> None:
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append((await receive())["type"])

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        raise AssertionError("no response expected")

    scope = {"type": "http", "headers": [(b"transfer-encoding", b"chunked")]}
    asyncio.run(BodyLimitMiddleware(app, 1024)(scope, receive, send))

    assert seen == ["http.disconnect"]
