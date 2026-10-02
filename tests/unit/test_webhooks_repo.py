"""WebhookRepo: subscriptions, secrets, event filtering, no backfill, backoff, max attempts."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from imda.store.db import connect, migrate
from imda.store.webhooks_repo import (
    BACKOFF_SECONDS,
    ERROR_IN_FLIGHT,
    ERROR_UNSAFE_URL,
    IN_FLIGHT_TTL_SECONDS,
    MAX_ERROR_LENGTH,
    WebhookRepo,
    backoff_seconds,
    error_class,
    normalize_events,
)

T0 = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)
URL = "https://hook.example.com/in"


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "w.sqlite3")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> WebhookRepo:
    return WebhookRepo(conn)


def add_event(
    conn: sqlite3.Connection, event_id: str, name: str, when: dt.datetime, **payload: object
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO events (event_id, event, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (event_id, name, json.dumps(payload), when.isoformat()),
        )


def pending_ids(repo: WebhookRepo, now: dt.datetime, max_attempts: int = 3) -> list[str]:
    return [p.event.event_id for p in repo.pending_work(now, max_attempts=max_attempts)]


def test_create_returns_secret_once_and_list_hides_it(repo: WebhookRepo) -> None:
    sub, secret = repo.create_subscription(URL, ["fx.rates.published"], now=T0)
    assert len(secret) >= 43
    assert repo.get_secret(sub.subscription_id) == secret
    listed = repo.list_subscriptions()
    assert listed == [sub]
    assert secret not in repr(listed)
    assert not hasattr(listed[0], "secret")


def test_secrets_are_unique(repo: WebhookRepo) -> None:
    _, first = repo.create_subscription(URL, ["*"])
    _, second = repo.create_subscription(URL, ["*"])
    assert first != second


def test_get_subscription_and_missing(repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["holidays.updated"], now=T0)
    assert repo.get_subscription(sub.subscription_id) == sub
    assert repo.get_subscription("nope") is None
    assert repo.get_secret("nope") is None


def test_deactivate(repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    assert repo.deactivate(sub.subscription_id) is True
    assert repo.deactivate(sub.subscription_id) is False
    assert repo.deactivate("nope") is False
    assert repo.list_subscriptions() == []
    assert [s.active for s in repo.list_subscriptions(include_inactive=True)] == [False]


def test_deactivate_forgets_the_secret_and_redacts_the_url(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sub, _ = repo.create_subscription("https://hook.example.com/in?token=abc", ["*"], now=T0)
    repo.deactivate(sub.subscription_id)

    assert repo.get_secret(sub.subscription_id) == ""
    (stored,) = repo.list_subscriptions(include_inactive=True)
    assert stored.url == "https://hook.example.com/…"
    row = conn.execute("SELECT secret, url FROM webhook_subscriptions").fetchone()
    assert (row["secret"], "abc" in row["url"]) == ("", False)


def test_deactivating_twice_does_not_touch_the_row_again(repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    assert repo.deactivate(sub.subscription_id) is True
    assert repo.deactivate(sub.subscription_id) is False
    assert repo.get_secret(sub.subscription_id) == ""


def test_normalize_events() -> None:
    assert normalize_events(["source.degraded", "fx.rates.published", "source.degraded"]) == (
        "fx.rates.published",
        "source.degraded",
    )
    assert normalize_events(["fx.rates.published", "*"]) == ("*",)
    with pytest.raises(ValueError, match="empty"):
        normalize_events([])
    with pytest.raises(ValueError, match="unknown events"):
        normalize_events(["fx.rates.published", "bogus"])


def test_create_rejects_unknown_event(repo: WebhookRepo) -> None:
    with pytest.raises(ValueError, match="unknown events"):
        repo.create_subscription(URL, ["bogus"])
    assert repo.list_subscriptions(include_inactive=True) == []


def test_pending_filters_by_event_name(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    only_fx, _ = repo.create_subscription(URL, ["fx.rates.published"], now=T0)
    everything, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e_fx", "fx.rates.published", at(1))
    add_event(conn, "e_hol", "holidays.updated", at(2))
    pairs = {(p.subscription.subscription_id, p.event.event_id) for p in repo.pending_work(at(10))}
    assert pairs == {
        (only_fx.subscription_id, "e_fx"),
        (everything.subscription_id, "e_fx"),
        (everything.subscription_id, "e_hol"),
    }


def test_only_events_after_subscription_created(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    add_event(conn, "old", "fx.rates.published", at(-60))
    add_event(conn, "same", "fx.rates.published", T0)
    repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "new", "fx.rates.published", at(5))
    assert pending_ids(repo, at(10)) == ["new"]


def test_inactive_subscription_gets_no_work(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    repo.deactivate(sub.subscription_id)
    assert pending_ids(repo, at(10)) == []


def test_pending_carries_payload_and_next_attempt(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "source.degraded", at(1), source="fbil")
    (item,) = repo.pending_work(at(10))
    assert item.attempt == 1
    assert item.event.payload == {"source": "fbil"}


def test_backoff_function() -> None:
    assert BACKOFF_SECONDS == (30, 120, 600)
    assert [backoff_seconds(n) for n in (0, 1, 2, 3, 4, 9)] == [30, 30, 120, 600, 600, 600]


def test_backoff_schedule_between_attempts(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    sid = sub.subscription_id

    repo.record_delivery(sid, "e1", attempt=1, succeeded=False, status_code=500, now=at(100))
    assert pending_ids(repo, at(129)) == []
    assert pending_ids(repo, at(130)) == ["e1"]

    repo.record_delivery(sid, "e1", attempt=2, succeeded=False, error="Timeout", now=at(200))
    assert pending_ids(repo, at(319)) == []
    (item,) = repo.pending_work(at(320), max_attempts=4)
    assert item.attempt == 3

    repo.record_delivery(sid, "e1", attempt=3, succeeded=False, status_code=502, now=at(400))
    assert pending_ids(repo, at(10_000), max_attempts=3) == []  # max attempts reached
    assert pending_ids(repo, at(999), max_attempts=4) == []  # 400 + 600 = 1000
    assert pending_ids(repo, at(1000), max_attempts=4) == ["e1"]


def test_success_stops_work(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    repo.record_delivery(sub.subscription_id, "e1", attempt=1, succeeded=True, status_code=204)
    assert pending_ids(repo, at(10_000)) == []


def test_unsafe_url_is_terminal(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    repo.record_delivery(
        sub.subscription_id, "e1", attempt=1, succeeded=False, error=ERROR_UNSAFE_URL, now=at(5)
    )
    assert pending_ids(repo, at(10_000)) == []


def test_pending_is_per_subscription(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    a, _ = repo.create_subscription(URL, ["*"], now=T0)
    b, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    repo.record_delivery(a.subscription_id, "e1", attempt=1, succeeded=True, status_code=200)
    (item,) = repo.pending_work(at(10))
    assert item.subscription.subscription_id == b.subscription_id


def test_pending_orders_oldest_first_and_limits(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    repo.create_subscription(URL, ["*"], now=T0)
    for i, name in enumerate(["e3", "e1", "e2"]):
        add_event(conn, name, "fx.rates.published", at(30 - i))
    assert pending_ids(repo, at(100)) == ["e2", "e1", "e3"]
    assert len(repo.pending_work(at(100), limit=2)) == 2


def test_deliveries_newest_first_with_limit_and_truncated_error(repo: WebhookRepo) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    sid = sub.subscription_id
    # event rows are required by the foreign key
    with repo._conn:
        repo._conn.execute(
            "INSERT INTO events VALUES ('e1', 'fx.rates.published', '{}', ?)", (at(0).isoformat(),)
        )
    for n in range(1, 4):
        repo.record_delivery(sid, "e1", attempt=n, succeeded=False, error="x" * 900, now=at(n))
    found = repo.deliveries(sid, limit=2)
    assert [d.attempt for d in found] == [3, 2]
    assert len(found[0].error or "") == MAX_ERROR_LENGTH
    assert repo.deliveries("other") == []


# ------------------------------------------------------------------ claims (no double delivery)
def test_claim_inserts_an_in_flight_row_and_hides_the_pair_from_later_passes(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))

    (claim,) = repo.claim_work(at(10))

    assert (claim.item.event.event_id, claim.item.attempt) == ("e1", 1)
    (row,) = repo.deliveries(sub.subscription_id)
    assert (row.delivery_id, row.error, row.status_code, row.succeeded, row.attempt) == (
        claim.delivery_id,
        ERROR_IN_FLIGHT,
        None,
        False,
        1,
    )
    assert repo.claim_work(at(11)) == []
    assert pending_ids(repo, at(11)) == []
    assert not conn.in_transaction


def test_complete_delivery_updates_the_claimed_row(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    (claim,) = repo.claim_work(at(10))

    repo.complete_delivery(claim.delivery_id, succeeded=True, status_code=204, now=at(12))

    (row,) = repo.deliveries(sub.subscription_id)
    assert (row.succeeded, row.status_code, row.error, row.attempted_at) == (
        True,
        204,
        None,
        at(12).isoformat(),
    )
    assert pending_ids(repo, at(10_000)) == []


def test_failed_completion_follows_the_backoff(conn: sqlite3.Connection, repo: WebhookRepo) -> None:
    repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    (claim,) = repo.claim_work(at(10))
    repo.complete_delivery(
        claim.delivery_id, succeeded=False, status_code=500, error="http_status", now=at(10)
    )

    assert pending_ids(repo, at(39)) == []
    (again,) = repo.claim_work(at(40))
    assert again.item.attempt == 2


def test_a_stale_in_flight_row_counts_as_a_failed_attempt(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    repo.claim_work(at(10))  # the dispatcher "crashes" here

    just_before = at(10 + IN_FLIGHT_TTL_SECONDS - 1)
    assert pending_ids(repo, just_before) == []  # still claimed
    (item,) = repo.pending_work(at(10 + IN_FLIGHT_TTL_SECONDS + 1))
    assert item.attempt == 2  # the crashed attempt was counted


def test_stale_in_flight_rows_still_respect_max_attempts(
    conn: sqlite3.Connection, repo: WebhookRepo
) -> None:
    repo.create_subscription(URL, ["*"], now=T0)
    add_event(conn, "e1", "fx.rates.published", at(1))
    repo.claim_work(at(10))
    assert repo.pending_work(at(100_000), max_attempts=1) == []


def test_claim_respects_limit_and_rolls_back_on_error(
    conn: sqlite3.Connection, repo: WebhookRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub, _ = repo.create_subscription(URL, ["*"], now=T0)
    for n in range(3):
        add_event(conn, f"e{n}", "fx.rates.published", at(1 + n))
    assert len(repo.claim_work(at(10), limit=2)) == 2

    def boom(*_: object, **__: object) -> list[object]:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(repo, "pending_work", boom)
    with pytest.raises(RuntimeError):
        repo.claim_work(at(10))
    assert not conn.in_transaction
    assert len(repo.deliveries(sub.subscription_id)) == 2


def test_two_connections_never_claim_the_same_pair(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite3"
    first = connect(path)
    migrate(first)
    second = connect(path)
    try:
        WebhookRepo(first).create_subscription(URL, ["*"], now=T0)
        add_event(first, "e1", "fx.rates.published", at(1))
        claimed = [
            *WebhookRepo(first).claim_work(at(10)),
            *WebhookRepo(second).claim_work(at(10)),
        ]
        assert len(claimed) == 1
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("error", "status", "expected"),
    [
        (None, 200, None),
        ("timeout", None, "timeout"),
        ("connect_error", None, "connect_error"),
        ("http_status", 500, "http_status"),
        ("unsafe_url", None, "unsafe_url"),
        ("in_flight", None, "in_flight"),
        ("ConnectError: refused 10.0.0.5:22", None, "connect_error"),
        ("HTTP 500", 500, "http_status"),
        ("ReadTimeout", None, "timeout"),
    ],
)
def test_error_class_is_coarse(error: str | None, status: int | None, expected: str | None) -> None:
    assert error_class(error, status) == expected
