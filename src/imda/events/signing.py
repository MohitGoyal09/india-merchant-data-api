"""HMAC-SHA256 webhook signing.

Scheme (same family as Razorpay's ``X-Razorpay-Signature``, plus a timestamp against replay):

    signature = hex(HMAC_SHA256(secret, f"{timestamp}." + raw_body))

Each delivery carries four headers: ``X-IMDA-Signature``, ``X-IMDA-Timestamp`` (unix seconds),
``X-IMDA-Event-Id`` and ``X-IMDA-Event``. Receivers must verify the RAW request bytes, before
any JSON parsing, and reject a timestamp more than 5 minutes (``DEFAULT_TOLERANCE_SECONDS``)
away from their own clock.

Receiver example::

    from imda.events.signing import verify

    def handle(request):  # any framework: you need the raw bytes and the headers
        ok = verify(
            SECRET,  # shown once, when the subscription was created
            request.body,
            request.headers["X-IMDA-Signature"],
            timestamp=int(request.headers["X-IMDA-Timestamp"]),
        )
        if not ok:
            return 401
        ...  # de-duplicate on X-IMDA-Event-Id, then process

Without ``timestamp`` the signature covers the body only (the plain Razorpay scheme). The
dispatcher always sends the timestamp.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-IMDA-Signature"
TIMESTAMP_HEADER = "X-IMDA-Timestamp"
EVENT_ID_HEADER = "X-IMDA-Event-Id"
EVENT_HEADER = "X-IMDA-Event"
DEFAULT_TOLERANCE_SECONDS = 300


def _signed_payload(body: bytes, timestamp: int | None) -> bytes:
    return body if timestamp is None else f"{timestamp}.".encode() + body


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Hex HMAC-SHA256 of ``body`` (prefixed with ``"{timestamp}."`` when a timestamp is given).

    Raises ``ValueError`` for an empty secret (a deleted subscription forgets its secret).
    """
    if not secret:
        raise ValueError("signing secret must not be empty")
    return hmac.new(
        secret.encode("utf-8"), _signed_payload(body, timestamp), hashlib.sha256
    ).hexdigest()


def verify(
    secret: str,
    body: bytes,
    signature: str,
    timestamp: int | None = None,
    now: float | None = None,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Constant-time check of ``signature``. With ``timestamp``, also reject stale or future ones.

    Returns False (never raises) for any mismatch, including a malformed signature or an
    empty secret.
    """
    if not secret:
        return False
    if timestamp is not None:
        current = time.time() if now is None else now
        if abs(current - timestamp) > tolerance:
            return False
    expected = sign(secret, body, timestamp)
    # "replace": a signature that is not valid UTF-8 text (a lone surrogate) can never match hex.
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8", "replace"))
