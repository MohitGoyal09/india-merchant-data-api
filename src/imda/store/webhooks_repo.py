"""Webhook subscriptions, delivery log and the retry queue, over the existing SQLite tables.

Nothing extra is stored for retries: whether a (subscription, event) pair is due is derived
from its ``webhook_deliveries`` rows (attempt count, last attempt time, success, terminal error).
``claim_work`` writes an ``in_flight`` row per pair BEFORE anything is sent, inside one
``BEGIN IMMEDIATE`` transaction, so two dispatchers never pick the same pair; the row is updated
with the outcome afterwards. The signing secret leaves this module only from
``create_subscription`` and ``get_secret``; deactivating a subscription erases it.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from imda.events.ssrf import redact_url

ALLOWED_EVENTS = (
    "fx.rates.published",
    "holidays.updated",
    "holidays.year_available",
    "source.degraded",
    "source.recovered",
)
WILDCARD = "*"

# Wait after attempt 1, 2, 3 (later attempts reuse the last value).
BACKOFF_SECONDS = (30, 120, 600)
MAX_ERROR_LENGTH = 500
# Coarse error classes: all a delivery row (and so the listing API) may carry. Never an
# exception message, which can reveal what a target host answered (a port-scan oracle).
ERROR_UNSAFE_URL = "unsafe_url"  # terminal: such a pair is never retried
ERROR_IN_FLIGHT = "in_flight"
ERROR_TIMEOUT = "timeout"
ERROR_CONNECT = "connect_error"
ERROR_HTTP_STATUS = "http_status"
ERROR_CLASSES = frozenset(
    {ERROR_UNSAFE_URL, ERROR_IN_FLIGHT, ERROR_TIMEOUT, ERROR_CONNECT, ERROR_HTTP_STATUS}
)
IN_FLIGHT_TTL_SECONDS = 600
"""An ``in_flight`` row younger than this is a live claim; an older one is a failed attempt."""

JsonDict = dict[str, object]


@dataclass(frozen=True, slots=True)
class Subscription:
    """A subscription as exposed to callers. It never carries the secret."""

    subscription_id: str
    url: str
    events: tuple[str, ...]
    active: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    event_id: str
    event: str
    payload: JsonDict
    created_at: str


@dataclass(frozen=True, slots=True)
class Delivery:
    delivery_id: str
    subscription_id: str
    event_id: str
    attempt: int
    status_code: int | None
    error: str | None
    attempted_at: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    """A (subscription, event) pair that is due. ``attempt`` is the number of the next try."""

    subscription: Subscription
    event: WebhookEvent
    attempt: int


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    """A pair claimed for sending; ``delivery_id`` is its in-flight row."""

    delivery_id: str
    item: PendingDelivery


def error_class(error: str | None, status_code: int | None) -> str | None:
    """Reduce any stored error text to one coarse class (rows written by older versions too)."""
    if error is None:
        return None
    if error in ERROR_CLASSES:
        return error
    if "timeout" in error.lower():
        return ERROR_TIMEOUT
    return ERROR_HTTP_STATUS if status_code is not None else ERROR_CONNECT


def backoff_seconds(attempts_made: int) -> int:
    """Seconds to wait after ``attempts_made`` failed attempts (1 -> 30, 2 -> 120, 3+ -> 600)."""
    return BACKOFF_SECONDS[min(max(attempts_made, 1), len(BACKOFF_SECONDS)) - 1]


def normalize_events(events: Iterable[str]) -> tuple[str, ...]:
    """Validate event names; ``*`` means all. Returns a sorted, de-duplicated tuple."""
    wanted = set(events)
    if not wanted:
        raise ValueError("events must not be empty")
    if WILDCARD in wanted:
        return (WILDCARD,)
    unknown = sorted(wanted - set(ALLOWED_EVENTS))
    if unknown:
        raise ValueError(f"unknown events {unknown}; allowed: {list(ALLOWED_EVENTS)} or '*'")
    return tuple(sorted(wanted))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat()


def _subscription(row: sqlite3.Row) -> Subscription:
    return Subscription(
        subscription_id=row["subscription_id"],
        url=row["url"],
        events=tuple(json.loads(row["events_json"])),
        active=bool(row["active"]),
        created_at=row["created_at"],
    )


def _delivery(row: sqlite3.Row) -> Delivery:
    return Delivery(
        delivery_id=row["delivery_id"],
        subscription_id=row["subscription_id"],
        event_id=row["event_id"],
        attempt=row["attempt"],
        status_code=row["status_code"],
        error=row["error"],
        attempted_at=row["attempted_at"],
        succeeded=bool(row["succeeded"]),
    )


class WebhookRepo:
    """Typed access to the webhook tables. Not shared across connections."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------ subscriptions
    def create_subscription(
        self, url: str, events: Iterable[str], *, now: dt.datetime | None = None
    ) -> tuple[Subscription, str]:
        """Create a subscription. The secret is returned here and is never listed again."""
        names = normalize_events(events)
        secret = secrets.token_urlsafe(32)
        subscription_id = f"whs_{uuid.uuid4().hex}"
        created_at = _iso(now or _now())
        with self._conn:
            self._conn.execute(
                "INSERT INTO webhook_subscriptions"
                " (subscription_id, url, events_json, secret, active, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?)",
                (subscription_id, url, json.dumps(list(names)), secret, created_at),
            )
        return Subscription(subscription_id, url, names, True, created_at), secret

    def list_subscriptions(self, *, include_inactive: bool = False) -> list[Subscription]:
        columns = "SELECT subscription_id, url, events_json, active, created_at"  # no secret
        order = " FROM webhook_subscriptions"
        if include_inactive:
            rows = self._conn.execute(columns + order + " ORDER BY created_at, rowid").fetchall()
        else:
            rows = self._conn.execute(
                columns + order + " WHERE active = 1 ORDER BY created_at, rowid"
            ).fetchall()
        return [_subscription(r) for r in rows]

    def get_subscription(self, subscription_id: str) -> Subscription | None:
        row = self._conn.execute(
            "SELECT subscription_id, url, events_json, active, created_at"
            " FROM webhook_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        return None if row is None else _subscription(row)

    def get_secret(self, subscription_id: str) -> str | None:
        """Internal: the signing secret. Never expose this through the API."""
        row = self._conn.execute(
            "SELECT secret FROM webhook_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()
        return None if row is None else str(row["secret"])

    def deactivate(self, subscription_id: str) -> bool:
        """Stop deliveries and forget the secret; the URL is cut to ``scheme://host/…``.

        True if an active subscription was switched off. The schema needs a non-null secret, so
        it becomes ``''`` (signing refuses an empty secret).
        """
        row = self._conn.execute(
            "SELECT url FROM webhook_subscriptions WHERE subscription_id = ? AND active = 1",
            (subscription_id,),
        ).fetchone()
        if row is None:
            return False
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE webhook_subscriptions SET active = 0, secret = '', url = ?"
                " WHERE subscription_id = ? AND active = 1",
                (redact_url(row["url"]), subscription_id),
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------ delivery log
    def record_delivery(
        self,
        subscription_id: str,
        event_id: str,
        *,
        attempt: int,
        succeeded: bool,
        status_code: int | None = None,
        error: str | None = None,
        now: dt.datetime | None = None,
    ) -> Delivery:
        clipped = None if error is None else error[:MAX_ERROR_LENGTH]
        delivery = Delivery(
            delivery_id=f"dlv_{uuid.uuid4().hex}",
            subscription_id=subscription_id,
            event_id=event_id,
            attempt=attempt,
            status_code=status_code,
            error=clipped,
            attempted_at=_iso(now or _now()),
            succeeded=succeeded,
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO webhook_deliveries (delivery_id, subscription_id, event_id, attempt,"
                " status_code, error, attempted_at, succeeded) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery.delivery_id,
                    subscription_id,
                    event_id,
                    attempt,
                    status_code,
                    clipped,
                    delivery.attempted_at,
                    int(succeeded),
                ),
            )
        return delivery

    def complete_delivery(
        self,
        delivery_id: str,
        *,
        succeeded: bool,
        status_code: int | None = None,
        error: str | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        """Write the outcome of a claimed attempt into its in-flight row."""
        clipped = None if error is None else error[:MAX_ERROR_LENGTH]
        with self._conn:
            self._conn.execute(
                "UPDATE webhook_deliveries SET attempted_at = ?, status_code = ?, error = ?,"
                " succeeded = ? WHERE delivery_id = ?",
                (_iso(now or _now()), status_code, clipped, int(succeeded), delivery_id),
            )

    def deliveries(self, subscription_id: str, limit: int = 50) -> list[Delivery]:
        """Newest first."""
        rows = self._conn.execute(
            "SELECT * FROM webhook_deliveries WHERE subscription_id = ?"
            " ORDER BY attempted_at DESC, rowid DESC LIMIT ?",
            (subscription_id, limit),
        ).fetchall()
        return [_delivery(r) for r in rows]

    # ------------------------------------------------------------ retry queue
    def claim_work(
        self, now: dt.datetime, *, max_attempts: int = 3, limit: int = 100
    ) -> list[ClaimedDelivery]:
        """Pick the due pairs and mark each ``in_flight`` in one write transaction.

        ``BEGIN IMMEDIATE`` takes the write lock first, so a second dispatcher (another thread
        or process) waits, then sees the claims and skips those pairs.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            claimed: list[ClaimedDelivery] = []
            for item in self.pending_work(now, max_attempts=max_attempts, limit=limit):
                delivery_id = f"dlv_{uuid.uuid4().hex}"
                self._conn.execute(
                    "INSERT INTO webhook_deliveries (delivery_id, subscription_id, event_id,"
                    " attempt, status_code, error, attempted_at, succeeded)"
                    " VALUES (?, ?, ?, ?, NULL, ?, ?, 0)",
                    (
                        delivery_id,
                        item.subscription.subscription_id,
                        item.event.event_id,
                        item.attempt,
                        ERROR_IN_FLIGHT,
                        _iso(now),
                    ),
                )
                claimed.append(ClaimedDelivery(delivery_id, item))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return claimed

    def pending_work(
        self, now: dt.datetime, *, max_attempts: int = 3, limit: int = 100
    ) -> list[PendingDelivery]:
        """Pairs that still need a delivery attempt, oldest event first.

        A pair is pending when the subscription is active and wants the event, the event is
        newer than the subscription (no backfill), no attempt succeeded, the last error was
        not terminal (``unsafe_url``), no live claim exists (an ``in_flight`` row younger than
        ``IN_FLIGHT_TTL_SECONDS``; an older one is a crashed attempt and counts as failed),
        fewer than ``max_attempts`` were made, and the backoff since the last attempt has passed.
        """
        work: list[PendingDelivery] = []
        live_after = now - dt.timedelta(seconds=IN_FLIGHT_TTL_SECONDS)
        for subscription in self.list_subscriptions():
            for event, attempts, last_at, claimed_at in self._candidates(subscription):
                if attempts >= max_attempts:
                    continue
                if claimed_at is not None and claimed_at > live_after:
                    continue
                if attempts and now < last_at + dt.timedelta(seconds=backoff_seconds(attempts)):
                    continue
                work.append(PendingDelivery(subscription, event, attempts + 1))
        work.sort(key=lambda item: (item.event.created_at, item.subscription.subscription_id))
        return work[:limit]

    def _candidates(
        self, subscription: Subscription
    ) -> list[tuple[WebhookEvent, int, dt.datetime, dt.datetime | None]]:
        """Undelivered, non-terminal events for one subscription with their attempt history:
        (event, attempts, last attempt time, time of the newest in-flight claim or ``None``)."""
        sql = (
            "SELECT e.event_id, e.event, e.payload_json, e.created_at,"
            " COUNT(d.delivery_id) AS attempts, MAX(d.attempted_at) AS last_at,"
            " COALESCE(MAX(d.succeeded), 0) AS ok,"
            " COALESCE(MAX(d.error = ?), 0) AS dead,"
            " MAX(CASE WHEN d.error = ? THEN d.attempted_at END) AS claimed_at"
            " FROM events e LEFT JOIN webhook_deliveries d"
            " ON d.event_id = e.event_id AND d.subscription_id = ?"
            " WHERE e.created_at > ?"
        )
        args: list[object] = [
            ERROR_UNSAFE_URL,
            ERROR_IN_FLIGHT,
            subscription.subscription_id,
            subscription.created_at,
        ]
        if WILDCARD not in subscription.events:
            sql += f" AND e.event IN ({','.join('?' * len(subscription.events))})"
            args.extend(subscription.events)
        sql += " GROUP BY e.event_id ORDER BY e.created_at, e.rowid"
        found: list[tuple[WebhookEvent, int, dt.datetime, dt.datetime | None]] = []
        for row in self._conn.execute(sql, args).fetchall():
            if row["ok"] or row["dead"]:
                continue
            event = WebhookEvent(
                event_id=row["event_id"],
                event=row["event"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            last = dt.datetime.fromisoformat(row["last_at"]) if row["last_at"] else _EPOCH
            claimed = dt.datetime.fromisoformat(row["claimed_at"]) if row["claimed_at"] else None
            found.append((event, row["attempts"], last, claimed))
        return found


_EPOCH = dt.datetime.fromtimestamp(0, dt.UTC)
