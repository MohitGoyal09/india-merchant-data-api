"""Webhook dispatcher: signed POSTs with retries, an SSRF re-check and a delivery log.

``dispatch_pending`` is one pass. Run it from a scheduler or ``imda webhooks dispatch``;
retry timing comes from ``WebhookRepo.pending_work`` (30 s, 2 min, 10 min backoff).
Secrets, URLs and bodies are never logged or stored in the delivery log: errors are reduced to
an HTTP status and a coarse class (``timeout``, ``connect_error``, ``http_status``,
``unsafe_url``).

Each pass first claims its work (an ``in_flight`` row per pair, see ``WebhookRepo.claim_work``),
so overlapping passes, threads or processes never send the same event twice. Each delivery
resolves the host name once, checks the answer, and connects to that exact address.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from imda.config import Settings
from imda.events import signing
from imda.events.ssrf import (
    PinnedTarget,
    Resolver,
    UnsafeWebhookUrl,
    resolve_webhook_url,
    validate_webhook_url,
)
from imda.store.webhooks_repo import (
    ERROR_CONNECT,
    ERROR_HTTP_STATUS,
    ERROR_TIMEOUT,
    ERROR_UNSAFE_URL,
    ClaimedDelivery,
    PendingDelivery,
    Subscription,
    WebhookEvent,
    WebhookRepo,
)

log = logging.getLogger("imda.webhooks")

USER_AGENT = "imda-webhooks/0.1"
DEFAULT_BATCH_LIMIT = 100

Clock = Callable[[], dt.datetime]

_DISPATCH_LOCK = threading.Lock()
"""One dispatch pass at a time per process (the database claim covers other processes)."""


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """``sent`` counts network attempts (``succeeded`` + ``failed``); unsafe URLs are separate."""

    sent: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_unsafe: int = 0


def build_body(event: WebhookEvent) -> bytes:
    """Compact, key-sorted UTF-8 JSON. These exact bytes are signed and sent."""
    document = {
        "id": event.event_id,
        "event": event.event,
        "created_at": event.created_at,
        "payload": event.payload,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def build_headers(secret: str, event: WebhookEvent, body: bytes, timestamp: int) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        signing.SIGNATURE_HEADER: signing.sign(secret, body, timestamp),
        signing.TIMESTAMP_HEADER: str(timestamp),
        signing.EVENT_ID_HEADER: event.event_id,
        signing.EVENT_HEADER: event.event,
    }


def register_subscription(
    repo: WebhookRepo,
    settings: Settings,
    url: str,
    events: list[str],
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> tuple[Subscription, str]:
    """Validate the URL (SSRF) and event names, then create. Raises ``UnsafeWebhookUrl``
    or ``ValueError``. The secret in the result is shown to the caller only once."""
    safe_url = validate_webhook_url(
        url, allow_private=settings.allow_private_webhooks, resolver=resolver
    )
    return repo.create_subscription(safe_url, events)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _attempt(
    http: httpx.Client,
    settings: Settings,
    secret: str,
    work: PendingDelivery,
    target: PinnedTarget,
    clock: Clock,
) -> tuple[int | None, str | None]:
    """POST once to the pinned address. Returns (status_code, error class); error is None on
    success. The signing timestamp is read from ``clock`` right before the request."""
    body = build_body(work.event)
    headers = build_headers(secret, work.event, body, int(clock().timestamp()))
    headers["Host"] = target.host_header
    extensions: dict[str, object] = {}
    if target.sni_hostname is not None:
        extensions["sni_hostname"] = target.sni_hostname  # TLS still checks the real name
    try:
        with http.stream(
            "POST",
            target.url,
            content=body,
            headers=headers,
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,
            extensions=extensions,
        ) as response:
            status = response.status_code
    except httpx.TimeoutException:
        return None, ERROR_TIMEOUT
    except (httpx.HTTPError, httpx.InvalidURL):
        return None, ERROR_CONNECT  # no message: it can carry the URL or reveal the target
    if 200 <= status < 300:
        return status, None
    return status, ERROR_HTTP_STATUS


def dispatch_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    http: httpx.Client | None = None,
    now: dt.datetime | None = None,
    clock: Clock | None = None,
    resolver: Resolver = socket.getaddrinfo,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> DispatchReport:
    """Make one delivery pass over everything that is due.

    ``now`` fixes the moment used to pick due work (default: the clock). ``clock`` supplies
    the time of each POST and of each log row; without it, a given ``now`` is used as is.
    """
    read: Clock = clock or ((lambda: now) if now is not None else _utc_now)
    with _DISPATCH_LOCK:
        return _dispatch_locked(conn, settings, http, now or read(), read, resolver, limit)


def _dispatch_locked(
    conn: sqlite3.Connection,
    settings: Settings,
    http: httpx.Client | None,
    moment: dt.datetime,
    clock: Clock,
    resolver: Resolver,
    limit: int,
) -> DispatchReport:
    repo = WebhookRepo(conn)
    claimed = repo.claim_work(moment, max_attempts=settings.webhook_max_attempts, limit=limit)
    if not claimed:
        return DispatchReport()
    owned = http is None
    client = http or httpx.Client(follow_redirects=False, trust_env=False)
    sent = succeeded = failed = skipped = 0
    try:
        for claim in claimed:
            outcome = _deliver_one(repo, client, settings, claim, clock, resolver)
            if outcome is None:
                skipped += 1
                continue
            sent += 1
            if outcome:
                succeeded += 1
            else:
                failed += 1
    finally:
        if owned:
            client.close()
    return DispatchReport(sent=sent, succeeded=succeeded, failed=failed, skipped_unsafe=skipped)


def _deliver_one(
    repo: WebhookRepo,
    client: httpx.Client,
    settings: Settings,
    claim: ClaimedDelivery,
    clock: Clock,
    resolver: Resolver,
) -> bool | None:
    """Send one claimed delivery and log the outcome. True/False for sent ok/failed, ``None``
    when it was skipped as unsafe."""
    item = claim.item
    sub_id, event_id = item.subscription.subscription_id, item.event.event_id
    secret = repo.get_secret(sub_id)
    try:
        if not secret:  # deleted since the claim: the secret is gone, never send unsigned
            raise UnsafeWebhookUrl("subscription has no secret")
        resolved = resolve_webhook_url(  # the ONE resolution of this delivery
            item.subscription.url, allow_private=settings.allow_private_webhooks, resolver=resolver
        )
    except UnsafeWebhookUrl:
        repo.complete_delivery(
            claim.delivery_id, succeeded=False, error=ERROR_UNSAFE_URL, now=clock()
        )
        log.warning("webhook skipped: unsafe url sub=%s event=%s", sub_id, event_id)
        return None
    status, error = _attempt(client, settings, secret, item, resolved.pin(), clock)
    repo.complete_delivery(
        claim.delivery_id, succeeded=error is None, status_code=status, error=error, now=clock()
    )
    log.info(
        "webhook sub=%s event=%s attempt=%d ok=%s status=%s",
        sub_id,
        event_id,
        item.attempt,
        error is None,
        status,
    )
    return error is None
