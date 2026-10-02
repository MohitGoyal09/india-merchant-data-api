"""Webhook subscriptions (admin token). The signing secret is shown once, at creation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from imda.api.auth import ADMIN_RESPONSES, AdminDeps
from imda.api.deps import Ctx, RequestContext
from imda.api.envelope import success
from imda.api.errors import ApiError
from imda.events import signing
from imda.events.ssrf import MAX_URL_LENGTH, UnsafeWebhookUrl
from imda.events.webhooks import register_subscription
from imda.store.repo import Store
from imda.store.webhooks_repo import (
    ALLOWED_EVENTS,
    WILDCARD,
    Delivery,
    Subscription,
    WebhookRepo,
    error_class,
)

router = APIRouter(
    prefix="/v1/webhooks", tags=["webhooks"], dependencies=AdminDeps, responses=ADMIN_RESPONSES
)

DEFAULT_DELIVERY_LIMIT = 50
MAX_DELIVERY_LIMIT = 500
_NO_STORE = {"Cache-Control": "no-store"}
VERIFY_EXAMPLE = f"""\
Each delivery is a POST of the raw JSON body with these headers:
  {signing.SIGNATURE_HEADER}: hex HMAC-SHA256 of "<timestamp>." + raw body, keyed with the secret
  {signing.TIMESTAMP_HEADER}: unix seconds when the delivery was signed
  {signing.EVENT_ID_HEADER}: unique event id (use it to de-duplicate)
  {signing.EVENT_HEADER}: event name
Verify the RAW bytes before you parse the JSON, and reject a timestamp more than
{signing.DEFAULT_TOLERANCE_SECONDS} seconds from your clock:

import hashlib, hmac, time

def verify(secret: str, raw_body: bytes, signature: str, timestamp: str) -> bool:
    if abs(time.time() - int(timestamp)) > {signing.DEFAULT_TOLERANCE_SECONDS}:
        return False
    expected = hmac.new(
        secret.encode(), f"{{timestamp}}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
"""


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=1, max_length=MAX_URL_LENGTH, description="https URL that receives events"
    )
    events: list[str] = Field(
        default_factory=lambda: [WILDCARD],
        description=f"Any of {', '.join(ALLOWED_EVENTS)}, or '*' for all",
    )


def subscription_view(subscription: Subscription) -> dict[str, object]:
    """Public view of a subscription. It has no secret field."""
    return {
        "id": subscription.subscription_id,
        "url": subscription.url,
        "events": list(subscription.events),
        "active": subscription.active,
        "created_at": subscription.created_at,
    }


def delivery_view(delivery: Delivery) -> dict[str, object]:
    """Public view of a delivery: the HTTP status and a coarse error class, never exception
    text (which could show what an internal host answered)."""
    return {
        "delivery_id": delivery.delivery_id,
        "subscription_id": delivery.subscription_id,
        "event_id": delivery.event_id,
        "attempt": delivery.attempt,
        "status_code": delivery.status_code,
        "error": error_class(delivery.error, delivery.status_code),
        "attempted_at": delivery.attempted_at,
        "succeeded": delivery.succeeded,
    }


@contextmanager
def writable_repo(ctx: RequestContext) -> Iterator[WebhookRepo]:
    """A repo on a read-write connection. Requests get a read-only store, so writes open their
    own (the schema already exists, so nothing is migrated)."""
    with Store.open(ctx.settings.db_path, migrate=False) as store:
        yield WebhookRepo(store.connection)


def _not_found(subscription_id: str) -> ApiError:
    return ApiError(
        404,
        "WEBHOOK_NOT_FOUND",
        f"Unknown webhook subscription {subscription_id!r}",
        {"id": subscription_id},
    )


@router.post("", status_code=201, summary="Create a subscription (secret shown once)")
def create_webhook(body: WebhookIn, ctx: Ctx) -> JSONResponse:
    try:
        with writable_repo(ctx) as repo:
            subscription, secret = register_subscription(repo, ctx.settings, body.url, body.events)
    except UnsafeWebhookUrl as exc:
        raise ApiError(422, "UNSAFE_WEBHOOK_URL", str(exc)) from None
    except ValueError as exc:  # unknown or empty event names
        raise ApiError(422, "VALIDATION_ERROR", str(exc)) from None
    data = {
        **subscription_view(subscription),
        "secret": secret,
        "verify_example": VERIFY_EXAMPLE,
    }
    response = success(ctx, data, headers=_NO_STORE)
    response.status_code = 201
    return response


@router.get("", summary="List active subscriptions (no secrets)")
def list_webhooks(ctx: Ctx) -> JSONResponse:
    repo = WebhookRepo(ctx.store.connection)
    return success(ctx, [subscription_view(s) for s in repo.list_subscriptions()])


@router.delete(
    "/{subscription_id}", status_code=204, summary="Stop a subscription (its secret is erased)"
)
def delete_webhook(subscription_id: str, ctx: Ctx) -> Response:
    with writable_repo(ctx) as repo:
        deactivated = repo.deactivate(subscription_id)
    if not deactivated:
        raise _not_found(subscription_id)
    return Response(status_code=204)


@router.get("/{subscription_id}/deliveries", summary="Delivery log, newest first")
def list_deliveries(
    subscription_id: str,
    ctx: Ctx,
    limit: Annotated[int, Query(ge=1, le=MAX_DELIVERY_LIMIT)] = DEFAULT_DELIVERY_LIMIT,
) -> JSONResponse:
    repo = WebhookRepo(ctx.store.connection)
    if repo.get_subscription(subscription_id) is None:
        raise _not_found(subscription_id)
    return success(ctx, [delivery_view(d) for d in repo.deliveries(subscription_id, limit)])
