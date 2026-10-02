"""Admin bearer-token auth for the write endpoints.

The token comes from ``IMDA_ADMIN_TOKEN``. With no token set, write endpoints are off (503) so a
fresh deployment can never be open by mistake. The token is compared in constant time and is
never logged or echoed back.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from imda.api.errors import ApiError, ErrorEnvelope

ADMIN_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Missing or invalid admin token"},
    503: {"model": ErrorEnvelope, "description": "Admin endpoints are disabled (no token set)"},
}

_bearer = HTTPBearer(
    auto_error=False, description="Admin token from IMDA_ADMIN_TOKEN, sent as a Bearer token."
)


def _digest(value: str) -> bytes:
    """Fixed-length digest, so the comparison does not depend on the length or encoding."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "UNAUTHORIZED",
        "Missing or invalid admin token. Send 'Authorization: Bearer <token>'.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    configured = request.app.state.settings.admin_token
    secret = configured.get_secret_value() if configured is not None else ""
    if not secret:
        raise ApiError(
            503,
            "ADMIN_DISABLED",
            "Admin endpoints are disabled: set IMDA_ADMIN_TOKEN to enable write endpoints",
        )
    if credentials is None or not hmac.compare_digest(
        _digest(credentials.credentials), _digest(secret)
    ):
        raise _unauthorized()


AdminDeps = [Depends(require_admin)]
