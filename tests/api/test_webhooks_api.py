"""``/v1/webhooks``: create (secret once), list, delete, deliveries, SSRF guard."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from imda.config import Settings
from imda.store.repo import Store
from imda.store.webhooks_repo import WebhookRepo
from tests.api.conftest import MakeClient
from tests.api.helpers import assert_envelope, assert_error, body

TOKEN = "webhook-test-token-0123456789abcdef0123"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PUBLIC_URL = "https://93.184.216.34/hook"  # an IP literal: resolves with no network


@pytest.fixture
def api(make_client: MakeClient, db_path: Path) -> TestClient:
    settings = Settings(db_path=db_path, admin_token=TOKEN, _env_file=None)  # type: ignore[call-arg]
    return make_client(custom=settings)


def create(api: TestClient, **payload: object) -> dict[str, object]:
    parsed = body(
        api.post("/v1/webhooks", json={"url": PUBLIC_URL, **payload}, headers=HEADERS), 201
    )
    data: dict[str, object] = parsed["data"]
    return data


def test_create_returns_subscription_secret_and_verify_example(api: TestClient) -> None:
    response = api.post(
        "/v1/webhooks",
        json={"url": PUBLIC_URL, "events": ["source.degraded", "fx.rates.published"]},
        headers=HEADERS,
    )

    parsed = body(response, 201)
    assert_envelope(parsed)
    data = parsed["data"]
    assert data["id"].startswith("whs_")
    assert data["url"] == PUBLIC_URL
    assert data["events"] == ["fx.rates.published", "source.degraded"]
    assert data["active"] is True
    assert len(data["secret"]) >= 32
    assert "X-IMDA-Signature" in data["verify_example"]
    assert "hmac.compare_digest" in data["verify_example"]
    assert response.headers["Cache-Control"] == "no-store"


def test_events_default_to_wildcard(api: TestClient) -> None:
    assert create(api)["events"] == ["*"]


def test_secret_is_shown_once_and_absent_from_list(api: TestClient) -> None:
    created = create(api)

    listed = api.get("/v1/webhooks", headers=HEADERS)

    parsed = body(listed)
    assert_envelope(parsed)
    assert parsed["meta"]["count"] == 1
    assert [row["id"] for row in parsed["data"]] == [created["id"]]
    assert "secret" not in parsed["data"][0]
    assert str(created["secret"]) not in listed.text


def test_secret_in_response_matches_the_stored_one(api: TestClient, store: Store) -> None:
    created = create(api)
    assert WebhookRepo(store.connection).get_secret(str(created["id"])) == created["secret"]


def test_delete_deactivates_and_a_second_delete_is_404(api: TestClient) -> None:
    created = create(api)
    url = f"/v1/webhooks/{created['id']}"

    deleted = api.delete(url, headers=HEADERS)

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert body(api.get("/v1/webhooks", headers=HEADERS))["data"] == []
    assert_error(api.delete(url, headers=HEADERS), 404, "WEBHOOK_NOT_FOUND")


def test_delete_unknown_id_is_404(api: TestClient) -> None:
    assert_error(api.delete("/v1/webhooks/whs_nope", headers=HEADERS), 404, "WEBHOOK_NOT_FOUND")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://10.0.0.5/hook",
        "https://169.254.169.254/latest/meta-data",
        "http://93.184.216.34/hook",
        "ftp://93.184.216.34/hook",
        "https://user:pw@93.184.216.34/hook",
    ],
)
def test_unsafe_urls_are_rejected_422(api: TestClient, url: str) -> None:
    response = api.post("/v1/webhooks", json={"url": url}, headers=HEADERS)

    assert_error(response, 422, "UNSAFE_WEBHOOK_URL")
    assert body(api.get("/v1/webhooks", headers=HEADERS))["data"] == []


def test_private_targets_allowed_only_when_configured(
    make_client: MakeClient, db_path: Path
) -> None:
    settings = Settings(
        db_path=db_path,
        admin_token=TOKEN,
        allow_private_webhooks=True,
        _env_file=None,  # type: ignore[call-arg]
    )
    client = make_client(custom=settings)

    response = client.post("/v1/webhooks", json={"url": "http://127.0.0.1:9/hook"}, headers=HEADERS)

    assert response.status_code == 201


def test_unknown_event_is_a_validation_error(api: TestClient) -> None:
    response = api.post(
        "/v1/webhooks", json={"url": PUBLIC_URL, "events": ["bogus.event"]}, headers=HEADERS
    )

    parsed = assert_error(response, 422, "VALIDATION_ERROR")
    assert "bogus.event" in parsed["error"]["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"url": ""},
        {"url": PUBLIC_URL, "events": []},
        {"url": PUBLIC_URL, "extra": 1},
        {"url": PUBLIC_URL, "events": "*"},
    ],
)
def test_malformed_bodies_are_rejected(api: TestClient, payload: dict[str, object]) -> None:
    response = api.post("/v1/webhooks", json=payload, headers=HEADERS)
    assert response.status_code == 422


def test_deliveries_listing_is_newest_first_and_limited(api: TestClient, store: Store) -> None:
    sub_id = str(create(api)["id"])
    repo = WebhookRepo(store.connection)
    event_id = store.record_event("fx.rates.published", {"n": 1})
    for attempt in (1, 2, 3):
        repo.record_delivery(
            sub_id,
            event_id,
            attempt=attempt,
            succeeded=attempt == 3,
            status_code=200 if attempt == 3 else 500,
            error=None if attempt == 3 else "HTTP 500",
        )

    parsed = body(api.get(f"/v1/webhooks/{sub_id}/deliveries", headers=HEADERS))
    assert_envelope(parsed)
    assert [d["attempt"] for d in parsed["data"]] == [3, 2, 1]
    first = parsed["data"][0]
    assert first["succeeded"] is True
    assert first["status_code"] == 200
    assert set(first) == {
        "delivery_id",
        "subscription_id",
        "event_id",
        "attempt",
        "status_code",
        "error",
        "attempted_at",
        "succeeded",
    }

    limited = body(api.get(f"/v1/webhooks/{sub_id}/deliveries?limit=1", headers=HEADERS))
    assert [d["attempt"] for d in limited["data"]] == [3]
    assert limited["meta"]["count"] == 1


def test_deliveries_of_unknown_subscription_is_404(api: TestClient) -> None:
    response = api.get("/v1/webhooks/whs_nope/deliveries", headers=HEADERS)
    assert_error(response, 404, "WEBHOOK_NOT_FOUND")


@pytest.mark.parametrize("limit", ["0", "-1", "501", "abc"])
def test_deliveries_limit_is_validated(api: TestClient, limit: str) -> None:
    sub_id = str(create(api)["id"])
    response = api.get(f"/v1/webhooks/{sub_id}/deliveries?limit={limit}", headers=HEADERS)
    assert_error(response, 422, "INVALID_REQUEST")


def test_deliveries_show_only_a_coarse_error_class(api: TestClient, store: Store) -> None:
    sub_id = str(create(api)["id"])
    repo = WebhookRepo(store.connection)
    event_id = store.record_event("fx.rates.published", {"n": 1})
    leaks = [
        (None, "ConnectError: [Errno 111] Connection refused 10.0.0.5:22"),
        (500, "HTTP 500"),
        (None, "ReadTimeout"),
        (None, "in_flight"),
        (None, "unsafe_url"),
    ]
    for attempt, (status, error) in enumerate(leaks, start=1):
        repo.record_delivery(
            sub_id, event_id, attempt=attempt, succeeded=False, status_code=status, error=error
        )

    rows = body(api.get(f"/v1/webhooks/{sub_id}/deliveries", headers=HEADERS))["data"]

    assert [(r["status_code"], r["error"]) for r in reversed(rows)] == [
        (None, "connect_error"),
        (500, "http_status"),
        (None, "timeout"),
        (None, "in_flight"),
        (None, "unsafe_url"),
    ]


def test_delete_erases_the_secret_and_redacts_the_url(api: TestClient, store: Store) -> None:
    created = create(api)
    sub_id = str(created["id"])

    assert api.delete(f"/v1/webhooks/{sub_id}", headers=HEADERS).status_code == 204

    repo = WebhookRepo(store.connection)
    assert repo.get_secret(sub_id) == ""
    (stored,) = repo.list_subscriptions(include_inactive=True)
    assert stored.url == "https://93.184.216.34/…"
    assert str(created["secret"]) not in str(
        store.connection.execute("SELECT * FROM webhook_subscriptions").fetchall()[0][:]
    )
