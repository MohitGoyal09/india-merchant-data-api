"""Bearer-token guard for the HTTP transport (``Authorization: Bearer <IMDA_MCP_TOKEN>``).

The token is compared in constant time and never logged or echoed. stdio needs no token.
"""

from __future__ import annotations

import hashlib
import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

BEARER_PREFIX = "bearer "


def _digest(value: str) -> bytes:
    """Fixed-length digest, so the comparison does not depend on length or encoding."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def token_matches(header: str | None, token: str) -> bool:
    """True when ``header`` is ``Bearer <token>``. Constant time in the token value."""
    presented = ""
    if header is not None and header.lower().startswith(BEARER_PREFIX):
        presented = header[len(BEARER_PREFIX) :].strip()
    ok = hmac.compare_digest(_digest(presented), _digest(token))
    return ok and bool(presented)


class BearerAuthMiddleware:
    """Reject HTTP requests without the right bearer token with 401. Other scopes pass through."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("a bearer token is required")
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if token_matches(Headers(scope=scope).get("authorization"), self._token):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Missing or invalid token. Send 'Authorization: Bearer <token>'.",
                }
            },
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
