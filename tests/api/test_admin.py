"""Admin auth, ``POST /v1/admin/refresh`` and ``POST /v1/admin/webhooks/dispatch``."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from imda.api import auth
from imda.api.routes import admin
from imda.config import Settings
from imda.events.webhooks import DispatchReport
from imda.ingest.common import RunSummary
from imda.models import IST
from tests.api.conftest import FRESH_NOW, MakeClient
from tests.api.helpers import assert_error, body

TOKEN = "s3cret-admin-token-value"
PROTECTED = "/v1/webhooks"


@pytest.fixture
def admin_settings(db_path: Path) -> Settings:
    return Settings(db_path=db_path, admin_token=TOKEN, _env_file=None)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def fresh_gate(monkeypatch: pytest.MonkeyPatch) -> admin.RefreshGate:
    gate = admin.RefreshGate()
    monkeypatch.setattr(admin, "GATE", gate)
    return gate


@pytest.fixture
def admin_client(make_client: MakeClient, admin_settings: Settings) -> TestClient:
    return make_client(custom=admin_settings)


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ auth
def test_unset_token_disables_admin_endpoints_with_503(client: TestClient) -> None:
    response = client.get(PROTECTED, headers=bearer("anything"))

    parsed = assert_error(response, 503, "ADMIN_DISABLED")
    assert "IMDA_ADMIN_TOKEN" in parsed["error"]["message"]


def test_empty_token_counts_as_unset(make_client: MakeClient, db_path: Path) -> None:
    settings = Settings(db_path=db_path, admin_token="", _env_file=None)  # type: ignore[call-arg]
    assert_error(make_client(custom=settings).get(PROTECTED), 503, "ADMIN_DISABLED")


def test_missing_header_is_401_with_challenge(admin_client: TestClient) -> None:
    response = admin_client.get(PROTECTED)

    assert_error(response, 401, "UNAUTHORIZED")
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer wrong-token", "Bearer ", f"Basic {TOKEN}", TOKEN, f"Bearer {TOKEN}x"],
)
def test_wrong_credentials_are_401(admin_client: TestClient, header: str) -> None:
    response = admin_client.get(PROTECTED, headers={"Authorization": header})

    assert_error(response, 401, "UNAUTHORIZED")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_non_ascii_credentials_are_401_not_a_crash(admin_client: TestClient) -> None:
    response = admin_client.get(PROTECTED, headers={"Authorization": "Bearer é".encode("latin-1")})
    assert_error(response, 401, "UNAUTHORIZED")


def test_right_token_is_accepted_and_scheme_is_case_insensitive(admin_client: TestClient) -> None:
    assert admin_client.get(PROTECTED, headers=bearer()).status_code == 200
    lower = {"Authorization": f"bearer {TOKEN}"}
    assert admin_client.get(PROTECTED, headers=lower).status_code == 200


def test_token_never_reaches_logs_or_responses(
    admin_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    wrong = "attempted-secret-guess"

    bad = admin_client.get(PROTECTED, headers=bearer(wrong))
    good = admin_client.get(PROTECTED, headers=bearer())

    assert wrong not in caplog.text
    assert TOKEN not in caplog.text
    assert wrong not in bad.text
    assert TOKEN not in bad.text
    assert TOKEN not in good.text


def test_admin_checked_before_the_database_is_touched(
    make_client: MakeClient, admin_settings: Settings
) -> None:
    broken = admin_settings.model_copy(update={"db_path": Path("/nonexistent-dir/x/imda.sqlite3")})
    response = make_client(custom=broken, raise_server_exceptions=False).get(PROTECTED)
    assert response.status_code == 401


def test_digest_comparison_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    assert auth._digest("a") != auth._digest("b")
    assert len(auth._digest("a")) == len(auth._digest("a" * 1000))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/admin/refresh"),
        ("POST", "/v1/admin/webhooks/dispatch"),
        ("POST", "/v1/webhooks"),
        ("DELETE", "/v1/webhooks/x"),
        ("GET", "/v1/webhooks/x/deliveries"),
    ],
)
def test_every_write_endpoint_needs_the_token(
    admin_client: TestClient, method: str, path: str
) -> None:
    assert_error(admin_client.request(method, path), 401, "UNAUTHORIZED")


# ------------------------------------------------------------------ refresh
class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, settings: Settings, on_exchange: Any = None) -> None:
        self.settings, self.on_exchange = settings, on_exchange
        FakeClient.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def fake_refresh(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(store: Any, client: Any, *, today: dt.date, exchange_log: Any = None, **_: Any):
        calls.append({"store": store, "client": client, "today": today, "log": exchange_log})
        return RunSummary(run_id="run_x", status="ok")

    FakeClient.instances.clear()
    monkeypatch.setattr(admin, "refresh", fake)
    monkeypatch.setattr(admin, "PoliteClient", FakeClient)
    return calls


def test_refresh_returns_202_and_runs_in_background(
    admin_client: TestClient, fake_refresh: list[dict[str, Any]], fresh_gate: admin.RefreshGate
) -> None:
    response = admin_client.post("/v1/admin/refresh", headers=bearer())

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    [call] = fake_refresh
    assert call["today"] == FRESH_NOW.astimezone(IST).date()
    assert call["client"] is FakeClient.instances[0]
    assert call["log"] is FakeClient.instances[0].on_exchange  # exchanges are logged
    assert fresh_gate.try_start() is not None  # gate was released afterwards


def test_second_refresh_while_one_runs_is_409(
    admin_client: TestClient, fake_refresh: list[dict[str, Any]], fresh_gate: admin.RefreshGate
) -> None:
    assert fresh_gate.try_start() is not None  # a refresh is "running"

    response = admin_client.post("/v1/admin/refresh", headers=bearer())

    assert_error(response, 409, "REFRESH_IN_PROGRESS")
    assert fake_refresh == []


def test_gate_is_released_when_the_refresh_crashes(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fresh_gate: admin.RefreshGate,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> RunSummary:
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(admin, "refresh", boom)
    monkeypatch.setattr(admin, "PoliteClient", FakeClient)

    assert admin_client.post("/v1/admin/refresh", headers=bearer()).status_code == 202

    assert "admin refresh crashed" in caplog.text
    assert fresh_gate.try_start() is not None


def test_gate_lease_expires_and_stale_finish_is_ignored() -> None:
    now = [0.0]
    gate = admin.RefreshGate(lease_seconds=10, clock=lambda: now[0])
    first = gate.try_start()
    assert first is not None
    assert gate.try_start() is None

    now[0] = 11.0
    second = gate.try_start()
    assert second is not None
    assert second != first
    gate.finish(first)  # a late finish from the old job must not free the new one
    assert gate.try_start() is None
    gate.finish(second)
    assert gate.try_start() is not None


# ------------------------------------------------------------------ dispatch
def test_dispatch_with_nothing_pending_returns_zero_report(admin_client: TestClient) -> None:
    response = admin_client.post("/v1/admin/webhooks/dispatch", headers=bearer())

    assert body(response) == {"sent": 0, "succeeded": 0, "failed": 0, "skipped_unsafe": 0}


def test_dispatch_returns_the_dispatch_report(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(conn: Any, settings: Settings, **_: Any) -> DispatchReport:
        seen["conn"], seen["settings"] = conn, settings
        return DispatchReport(sent=3, succeeded=2, failed=1, skipped_unsafe=1)

    monkeypatch.setattr(admin, "dispatch_pending", fake)

    response = admin_client.post("/v1/admin/webhooks/dispatch", headers=bearer())

    assert body(response) == {"sent": 3, "succeeded": 2, "failed": 1, "skipped_unsafe": 1}
    assert seen["conn"] is not None
