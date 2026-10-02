"""Dispatcher: signing on the wire, retries, redirects, timeouts, SSRF re-check at send time."""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import sqlite3
import threading
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
from imda.store.webhooks_repo import (
    ERROR_IN_FLIGHT,
    ERROR_UNSAFE_URL,
    IN_FLIGHT_TTL_SECONDS,
    WebhookEvent,
    WebhookRepo,
)

T0 = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)
URL = "https://hook.example.com/in?token=urlsecret"
ENDPOINT = "https://93.184.216.34/in"  # the validated IP the dispatcher pins to


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
        "http_status",
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
    assert delivery.error == "http_status"


@pytest.mark.parametrize(
    ("exc", "name"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow"), "timeout"),
        (
            httpx.ConnectError("refused https://hook.example.com/in?token=urlsecret"),
            "connect_error",
        ),
        (httpx.RemoteProtocolError("bad 10.0.0.5:22"), "connect_error"),
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
    assert delivery.error == name  # a coarse class, never the exception text
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
    good = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))

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
    route = respx.post("http://127.0.0.1:9000/hook").mock(return_value=httpx.Response(200))
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


# ------------------------------------------------------------------ DNS pinning
@respx.mock
def test_request_goes_to_the_validated_ip_with_original_host_and_sni(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    subscribe(repo, url="https://hook.example.com:8443/in?token=urlsecret")
    emit(conn)
    route = respx.post("https://93.184.216.34:8443/in").mock(return_value=httpx.Response(200))

    assert run(conn, at(10)).succeeded == 1

    request = route.calls.last.request
    assert request.url.host == "93.184.216.34"
    assert request.url.port == 8443
    assert request.url.params["token"] == "urlsecret"
    assert request.headers["Host"] == "hook.example.com:8443"
    assert request.extensions["sni_hostname"] == "hook.example.com"


@respx.mock
def test_ipv6_target_is_bracketed(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    subscribe(repo)
    emit(conn)
    route = respx.post("https://[2606:4700::1111]/in").mock(return_value=httpx.Response(200))

    assert run(conn, at(10), resolver=resolving_to("2606:4700::1111")).succeeded == 1

    assert route.calls.last.request.headers["Host"] == "hook.example.com"


@respx.mock
def test_one_resolution_per_delivery_so_rebinding_cannot_redirect(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    subscribe(repo)
    emit(conn)
    calls: list[str] = []

    def flipping(host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        calls.append(host)
        address = "93.184.216.34" if len(calls) == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    good = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    evil = respx.route(host="169.254.169.254").mock(return_value=httpx.Response(200))

    assert run(conn, at(10), resolver=flipping).succeeded == 1

    assert calls == ["hook.example.com"]
    assert good.call_count == 1
    assert not evil.called


@respx.mock
def test_ip_literal_url_sends_no_sni_override(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    subscribe(repo, url=ENDPOINT)
    emit(conn)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    assert run(conn, at(10)).succeeded == 1
    assert "sni_hostname" not in route.calls.last.request.extensions


# ------------------------------------------------------------------ per-attempt timestamp
@respx.mock
def test_timestamp_is_taken_at_each_post_not_at_pass_start(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    _, secret = subscribe(repo)
    emit(conn, "fx.rates.published", n=1)
    emit(conn, "fx.rates.published", n=2)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    # per delivery the clock is read for the POST, then for the log row
    ticks = iter(at(t) for t in (100, 130, 160, 190))

    report = dispatch_pending(
        conn, make_settings(), now=at(10), resolver=PUBLIC, clock=lambda: next(ticks)
    )

    assert report.succeeded == 2
    stamps = [int(c.request.headers[signing.TIMESTAMP_HEADER]) for c in route.calls]
    assert stamps == [int(at(100).timestamp()), int(at(160).timestamp())]
    for call, stamp in zip(route.calls, stamps, strict=True):
        assert signing.verify(
            secret,
            call.request.content,
            call.request.headers[signing.SIGNATURE_HEADER],
            timestamp=stamp,
            now=stamp,
        )


# ------------------------------------------------------------------ no double delivery
@respx.mock
def test_back_to_back_passes_deliver_each_event_once(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    subscribe(repo)
    emit(conn)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))

    first = run(conn, at(10))
    second = run(conn, at(10))

    assert (first.succeeded, second.sent) == (1, 0)
    assert route.call_count == 1


@respx.mock
def test_concurrent_passes_on_separate_connections_deliver_once(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite3"
    setup = connect(path)
    migrate(setup)
    repo = WebhookRepo(setup)
    sid, _ = subscribe(repo)
    emit(setup)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    barrier = threading.Barrier(2)
    reports: list[DispatchReport] = []

    def worker() -> None:
        own = connect(path)
        try:
            barrier.wait()
            reports.append(run(own, at(10)))
        finally:
            own.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(r.sent for r in reports) == [0, 1]
    assert route.call_count == 1
    assert len(repo.deliveries(sid)) == 1
    setup.close()


@respx.mock
def test_an_in_flight_claim_blocks_another_pass_until_it_expires(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    repo.claim_work(at(10))  # another dispatcher took it and has not finished
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))

    assert run(conn, at(11)) == DispatchReport()
    assert not route.called

    # the claimer crashed: after the TTL it is a failed attempt and the pair is retried
    assert run(conn, at(10 + IN_FLIGHT_TTL_SECONDS + 1)).succeeded == 1
    attempts = sorted(d.attempt for d in repo.deliveries(sid))
    assert attempts == [1, 2]


@respx.mock
def test_the_claim_row_is_written_before_the_request_is_sent(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    seen: list[tuple[str | None, int | None]] = []

    def inspect(request: httpx.Request) -> httpx.Response:
        (row,) = repo.deliveries(sid)
        seen.append((row.error, row.status_code))
        return httpx.Response(200)

    respx.post(ENDPOINT).mock(side_effect=inspect)
    run(conn, at(10))

    assert seen == [(ERROR_IN_FLIGHT, None)]
    (done,) = repo.deliveries(sid)
    assert (done.error, done.succeeded) == (None, True)


@respx.mock
def test_deleted_subscription_with_no_secret_is_not_sent(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    claims = repo.claim_work(at(10))  # claimed, then deleted before the send
    repo.deactivate(sid)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    assert len(claims) == 1

    # a pass that picks it up after expiry finds nothing: the subscription is inactive
    assert run(conn, at(10_000)) == DispatchReport()
    assert not route.called


@respx.mock
def test_empty_secret_at_send_time_is_skipped_and_recorded(
    conn: sqlite3.Connection, repo: WebhookRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, _ = subscribe(repo)
    emit(conn)
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200))
    monkeypatch.setattr(WebhookRepo, "get_secret", lambda self, _id: "")

    report = run(conn, at(10))

    assert report == DispatchReport(skipped_unsafe=1)
    assert not route.called
    (delivery,) = repo.deliveries(sid)
    assert delivery.error == ERROR_UNSAFE_URL


def test_without_now_or_clock_the_real_utc_clock_is_used(conn: sqlite3.Connection) -> None:
    assert dispatch_pending(conn, make_settings(), resolver=PUBLIC) == DispatchReport()
