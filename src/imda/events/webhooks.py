"""Webhook dispatcher: signed POSTs with retries, an SSRF re-check and a delivery log.

``dispatch_pending`` is one pass. Run it from a scheduler or ``imda webhooks dispatch``;
retry timing comes from ``WebhookRepo.pending_work`` (30 s, 2 min, 10 min backoff).
Secrets, URLs and bodies are never logged or stored in the delivery log: errors are reduced to
an HTTP status or an exception class name.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import sqlite3
from dataclasses import dataclass

import httpx

from imda.config import Settings
from imda.events import signing
from imda.events.ssrf import Resolver, UnsafeWebhookUrl, validate_webhook_url
from imda.store.webhooks_repo import (
    ERROR_UNSAFE_URL,
    PendingDelivery,
    Subscription,
    WebhookEvent,
    WebhookRepo,
)

log = logging.getLogger("imda.webhooks")

USER_AGENT = "imda-webhooks/0.1"
DEFAULT_BATCH_LIMIT = 100


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


def _attempt(
    http: httpx.Client, settings: Settings, secret: str, work: PendingDelivery, now: dt.datetime
) -> tuple[int | None, str | None]:
    """POST once. Returns (status_code, error); error is None on success."""
    body = build_body(work.event)
    headers = build_headers(secret, work.event, body, int(now.timestamp()))
    try:
        with http.stream(
            "POST",
            work.subscription.url,
            content=body,
            headers=headers,
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,
        ) as response:
            status = response.status_code
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return None, type(exc).__name__  # no message: it can carry the URL
    if 200 <= status < 300:
        return status, None
    if 300 <= status < 400:
        return status, f"redirect not followed (HTTP {status})"
    return status, f"HTTP {status}"


def dispatch_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    http: httpx.Client | None = None,
    now: dt.datetime | None = None,
    resolver: Resolver = socket.getaddrinfo,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> DispatchReport:
    """Make one delivery pass over everything that is due."""
    moment = now or dt.datetime.now(dt.UTC)
    repo = WebhookRepo(conn)
    work = repo.pending_work(moment, max_attempts=settings.webhook_max_attempts, limit=limit)
    if not work:
        return DispatchReport()
    owned = http is None
    client = http or httpx.Client(follow_redirects=False, trust_env=False)
    sent = succeeded = failed = skipped = 0
    try:
        for item in work:
            sub_id, event_id = item.subscription.subscription_id, item.event.event_id
            secret = repo.get_secret(sub_id)
            if secret is None:  # pragma: no cover - subscription rows are never deleted
                continue
            try:
                validate_webhook_url(
                    item.subscription.url,
                    allow_private=settings.allow_private_webhooks,
                    resolver=resolver,
                )
            except UnsafeWebhookUrl:
                repo.record_delivery(
                    sub_id,
                    event_id,
                    attempt=item.attempt,
                    succeeded=False,
                    error=ERROR_UNSAFE_URL,
                    now=moment,
                )
                skipped += 1
                log.warning("webhook skipped: unsafe url sub=%s event=%s", sub_id, event_id)
                continue
            status, error = _attempt(client, settings, secret, item, moment)
            repo.record_delivery(
                sub_id,
                event_id,
                attempt=item.attempt,
                succeeded=error is None,
                status_code=status,
                error=error,
                now=moment,
            )
            sent += 1
            if error is None:
                succeeded += 1
            else:
                failed += 1
            log.info(
                "webhook sub=%s event=%s attempt=%d ok=%s status=%s",
                sub_id,
                event_id,
                item.attempt,
                error is None,
                status,
            )
    finally:
        if owned:
            client.close()
    return DispatchReport(sent=sent, succeeded=succeeded, failed=failed, skipped_unsafe=skipped)
