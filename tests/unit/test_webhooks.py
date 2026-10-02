"""Dispatcher: signing on the wire, retries, redirects, timeouts, SSRF re-check at send time."""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from imda.config import Settings
from imda.events import signing
from imda.events.ssrf import UnsafeWebhookUrl
from imda.events.webhooks import (
    DispatchReport,
    build_body,
    build_headers,
    dispatch_pending,
    register_subscription,
)
from imda.store.db import connect, migrate
from imda.store.repo import Store
from imda.store.webhooks_repo import ERROR_UNSAFE_URL, WebhookEvent, WebhookRepo

T0 = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)
URL = "https://hook.example.com/in?token=urlsecret"
ENDPOINT = "https://hook.example.com/in"


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def resolving_to(*addresses: str) -> Callable[..., list[tuple[Any, ...]]]:
    def resolver(host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)) for addr in addresses]

    return resolver


PUBLIC = resolving_to("93.184.216.34")
PRIVATE = resolving_to("169.254.169.254")


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "d.sqlite3")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> WebhookRepo:
    return WebhookRepo(conn)


def subscribe(
    repo: WebhookRepo, url: str = URL, events: tuple[str, ...] = ("fx.rates.published",)
) -> tuple[str, str]:
    sub, secret = repo.create_subscription(url, list(events), now=T0)
    return sub.subscription_id, secret


def emit(conn: sqlite3.Connection, name: str = "fx.rates.published", **payload: object) -> str:
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    event_id = f"evt_{count}"
    with conn:
        conn.execute(
            "INSERT INTO events (event_id, event, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (event_id, name, json.dumps(payload), at(1).isoformat()),
        )
    return event_id


def run(
    conn: sqlite3.Connection,
    now: dt.datetime,
    *,
    resolver: Callable[..., Any] = PUBLIC,
    settings: Settings | None = None,
    http: httpx.Client | None = None,
) -> DispatchReport:
    return dispatch_pending(
        conn, settings or make_settings(), http=http, now=now, resolver=resolver
    )


# ------------------------------------------------------------------ pure helpers
def test_build_body_is_compact_sorted_utf8() -> None:
    event = WebhookEvent(
        "evt_1", "holidays.updated", {"b": 1, "a": "₹"}, "2026-10-02T08:00:01+00:00"
    )
    body = build_body(event)
    assert body == (
        b'{"created_at":"2026-10-02T08:00:01+00:00","event":"holidays.updated",'
        b'"id":"evt_1","payload":{"a":"\xe2\x82\xb9","b":1}}'
    )
    assert json.loads(body)["id"] == "evt_1"


def test_build_headers_signature_covers_timestamp_and_body() -> None:
    event = WebhookEvent("evt_1", "source.degraded", {}, "2026-10-02T08:00:01+00:00")
    body = build_body(event)
    headers = build_headers("sek", event, body, 1_790_000_000)
    assert headers[signing.EVENT_ID_HEADER] == "evt_1"
    assert headers[signing.EVENT_HEADER] == "source.degraded"
    assert headers[signing.TIMESTAMP_HEADER] == "1790000000"
    assert headers[signing.SIGNATURE_HEADER] == signing.sign("sek", body, 1_790_000_000)
    assert headers["Content-Type"] == "application/json"
    assert "sek" not in json.dumps(headers)


# ------------------------------------------------------------------ dispatch
@respx.mock
def test_success_sends_signed_request_and_logs_delivery(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sid, secret = subscribe(repo)
    event_id = emit(conn, rate="95.9")
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(204))

    report = run(conn, at(10))

    assert report == DispatchReport(sent=1, succeeded=1, failed=0, skipped_unsafe=0)
    request = route.calls.last.request
    assert request.url.params["token"] == "urlsecret"
    body = request.content
    expected = WebhookEvent(event_id, "fx.rates.published", {"rate": "95.9"}, at(1).isoformat())
    assert body == build_body(expected)
    timestamp = int(request.headers[signing.TIMESTAMP_HEADER])
    assert timestamp == int(at(10).timestamp())
    assert signing.verify(
        secret, body, request.headers[signing.SIGNATURE_HEADER], timestamp=timestamp, now=timestamp
    )
    assert request.headers[signing.EVENT_ID_HEADER] == event_id
    assert request.headers[signing.EVENT_HEADER] == "fx.rates.published"
    (delivery,) = repo.deliveries(sid)
    assert (delivery.succeeded, delivery.status_code, delivery.error, delivery.attempt) == (
        True,
        204,
        None,
        1,
    )
    assert run(conn, at(1000)) == DispatchReport()  # delivered once only
    assert route.call_count == 1


@respx.mock
def test_caller_supplied_client_is_used_and_left_open(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    subscribe(repo)
    emit(conn)
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    with httpx.Client() as client:
        assert run(conn, at(10), http=client).succeeded == 1
        assert not client.is_closed


@respx.mock
def test_500_is_retried_after_backoff_then_succeeds(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    route = respx.post(ENDPOINT).mock(side_effect=[httpx.Response(500), httpx.Response(200)])

    assert run(conn, at(10)) == DispatchReport(sent=1, succeeded=0, failed=1)
    assert run(conn, at(39)) == DispatchReport()  # 30 s backoff not over
    assert route.call_count == 1
    assert run(conn, at(40)) == DispatchReport(sent=1, succeeded=1, failed=0)

    newest, oldest = repo.deliveries(sid)
    assert (oldest.attempt, oldest.status_code, oldest.error, oldest.succeeded) == (
        1,
        500,
        "HTTP 500",
        False,
    )
    assert (newest.attempt, newest.succeeded) == (2, True)


@respx.mock
def test_gives_up_after_max_attempts(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    subscribe(repo)
    emit(conn)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(503))
    totals = [run(conn, at(t)).sent for t in (10, 100, 1000, 10_000)]
    assert totals == [1, 1, 1, 0]
    assert route.call_count == 3


@respx.mock
def test_max_attempts_setting_is_honoured(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    subscribe(repo)
    emit(conn)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(500))
    settings = make_settings(webhook_max_attempts=1)
    assert run(conn, at(10), settings=settings).sent == 1
    assert run(conn, at(10_000), settings=settings).sent == 0
    assert route.call_count == 1


@respx.mock
def test_redirect_is_a_failure_and_never_followed(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
    )
    target = respx.route(host="169.254.169.254").mock(return_value=httpx.Response(200))

    report = run(conn, at(10))

    assert report.failed == 1
    assert not target.called
    (delivery,) = repo.deliveries(sid)
    assert delivery.status_code == 302
    assert delivery.error is not None
    assert "redirect" in delivery.error


@pytest.mark.parametrize(
    ("exc", "name"),
    [
        (httpx.ReadTimeout("slow"), "ReadTimeout"),
        (httpx.ConnectError("refused https://hook.example.com/in?token=urlsecret"), "ConnectError"),
    ],
)
@respx.mock
def test_transport_errors_are_failures_without_leaking_details(
    conn: sqlite3.Connection, repo: WebhookRepo, exc: Exception, name: str
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    respx.post(ENDPOINT).mock(side_effect=exc)

    assert run(conn, at(10)) == DispatchReport(sent=1, succeeded=0, failed=1)

    (delivery,) = repo.deliveries(sid)
    assert delivery.status_code is None
    assert delivery.error == name
    assert not delivery.succeeded


@respx.mock
def test_timeout_is_taken_from_settings(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    subscribe(repo)
    emit(conn)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    run(conn, at(10), settings=make_settings(webhook_timeout_seconds=3.5))
    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["read"] == 3.5
    assert timeout["connect"] == 3.5


@respx.mock
def test_unsafe_at_delivery_time_is_skipped_recorded_and_not_retried(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))

    # DNS rebinding: the name now resolves to the cloud metadata address.
    report = run(conn, at(10), resolver=PRIVATE)

    assert report == DispatchReport(sent=0, succeeded=0, failed=0, skipped_unsafe=1)
    assert not route.called
    (delivery,) = repo.deliveries(sid)
    assert (delivery.error, delivery.succeeded) == (ERROR_UNSAFE_URL, False)
    # Even when DNS looks fine again, a flagged pair is not retried.
    assert run(conn, at(100_000), resolver=PUBLIC) == DispatchReport()
    assert not route.called


@respx.mock
def test_other_subscriptions_still_deliver_when_one_is_unsafe(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    subscribe(repo, url="https://bad.example.com/in")
    subscribe(repo, url="https://good.example.com/in")
    emit(conn)
    good = respx.post("https://good.example.com/in").mock(return_value=httpx.Response(200))

    def split(host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        address = "10.0.0.1" if host.startswith("bad") else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    report = run(conn, at(10), resolver=split)

    assert report == DispatchReport(sent=1, succeeded=1, failed=0, skipped_unsafe=1)
    assert good.call_count == 1


@respx.mock
def test_allow_private_permits_local_http(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    subscribe(repo, url="http://localhost:9000/hook")
    emit(conn)
    route = respx.post("http://localhost:9000/hook").mock(return_value=httpx.Response(200))
    settings = make_settings(allow_private_webhooks=True)
    assert run(conn, at(10), resolver=resolving_to("127.0.0.1"), settings=settings).succeeded == 1
    assert route.called


@respx.mock
def test_event_filtering_only_delivers_subscribed_events(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    subscribe(repo, events=("holidays.updated",))
    emit(conn, "fx.rates.published")
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    assert run(conn, at(10)) == DispatchReport()
    assert not route.called


def test_dispatch_with_nothing_pending_never_builds_a_client(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        raise AssertionError("no client expected")

    monkeypatch.setattr(httpx, "Client", boom)
    assert run(conn, at(10)) == DispatchReport()


@respx.mock
def test_logs_contain_no_secret_url_or_body(
    conn: sqlite3.Connection, repo: WebhookRepo, caplog: pytest.LogCaptureFixture
) -> None:
    _, secret = subscribe(repo)
    emit(conn, rate="very-private-value")
    respx.post(ENDPOINT).mock(return_value=httpx.Response(500))
    with caplog.at_level(logging.DEBUG, logger="imda.webhooks"):
        run(conn, at(10))
        run(conn, at(10_000), resolver=PRIVATE)
    text = caplog.text
    assert "webhook sub=" in text
    for forbidden in (secret, "urlsecret", "very-private-value", "hook.example.com"):
        assert forbidden not in text


@respx.mock
def test_end_to_end_with_store_events(tmp_path: Path) -> None:
    """An event recorded through Store.record_event after subscribing is delivered."""
    with Store.open(tmp_path / "e2e.sqlite3") as store:
        conn = store._conn
        WebhookRepo(conn).create_subscription(URL, ["*"])  # created_at is the real now
        store.record_event("source.degraded", {"source": "rbi"})
        route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
        later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
        report = dispatch_pending(conn, make_settings(), now=later, resolver=PUBLIC)
        assert report.succeeded == 1
        assert json.loads(route.calls.last.request.content)["event"] == "source.degraded"


# ------------------------------------------------------------------ registration
def test_register_subscription_validates_url_and_events(repo: WebhookRepo) -> None:
    settings = make_settings()
    sub, secret = register_subscription(
        repo, settings, "HTTPS://Hook.Example.com/in#x", ["*"], resolver=PUBLIC
    )
    assert sub.url == "https://hook.example.com/in"
    assert repo.get_secret(sub.subscription_id) == secret
    with pytest.raises(UnsafeWebhookUrl):
        register_subscription(repo, settings, "https://x.example.com/", ["*"], resolver=PRIVATE)
    with pytest.raises(UnsafeWebhookUrl):
        register_subscription(repo, settings, "http://hook.example.com/", ["*"], resolver=PUBLIC)
    with pytest.raises(ValueError, match="unknown events"):
        register_subscription(
            repo, settings, "https://hook.example.com/", ["nope"], resolver=PUBLIC
        )
    assert len(repo.list_subscriptions()) == 1
