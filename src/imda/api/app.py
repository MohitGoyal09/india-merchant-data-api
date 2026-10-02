"""FastAPI application factory. Run with ``uvicorn --factory imda.api.app:create_app``."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from imda import __version__
from imda.api.console.routes import router as console_router
from imda.api.deps import SnapshotCache
from imda.api.errors import (
    ERROR_RESPONSES,
    error_response,
    install_error_handlers,
    internal_error_response,
    payload_too_large,
)
from imda.api.routes import (
    admin,
    calendar,
    fx,
    holidays,
    invoice,
    meta,
    mibor,
    offices,
    settlement,
    sources,
    webhooks,
)
from imda.config import Settings, get_settings
from imda.models import IST
from imda.observability import route_template
from imda.store.repo import Store

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_ACCESS_LOG = "imda.api.access"
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}

DESCRIPTION = (
    "RBI bank holidays, RBI/FBIL FX reference rates, MIBOR, settlement ETA and invoice quotes "
    "as one typed API. Data is served from a local store filled by `imda backfill` / "
    "`imda refresh`; every response carries provenance. Not affiliated with RBI, FBIL or "
    "Razorpay."
)
TAGS = [
    {"name": "meta", "description": "Service links and liveness."},
    {"name": "offices", "description": "The RBI regional offices."},
    {"name": "holidays", "description": "RBI bank holidays per regional office."},
    {"name": "calendar", "description": "Business-day engine and ICS feeds."},
    {"name": "settlement", "description": "Indicative settlement ETA."},
    {"name": "fx", "description": "FX reference rates: history, as-of, convert, stats, compare."},
    {"name": "invoice", "description": "Cross-border invoice quote."},
    {"name": "mibor", "description": "FBIL overnight MIBOR."},
    {"name": "sources", "description": "Upstream health: status, drift and freshness."},
    {"name": "webhooks", "description": "Signed event subscriptions (admin token)."},
    {"name": "admin", "description": "Refresh and dispatch actions (admin token)."},
]


def _now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _configure_access_log() -> logging.Logger:
    logger = logging.getLogger(_ACCESS_LOG)
    logger.setLevel(logging.INFO)
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def _request_id(request: Request) -> str:
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    return incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex


class BodyLimitMiddleware:
    """Reject request bodies over ``max_bytes`` with 413 before the app reads them.

    A ``Content-Length`` over the limit is refused outright. A chunked body (no length) is
    read here, up to the limit, then replayed to the app; going over refuses it.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None:
            if declared.isdigit() and int(declared) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
        elif "transfer-encoding" in headers:
            buffered = await self._buffer(receive)
            if buffered is None:
                await self._reject(scope, receive, send)
                return
            receive = _replay(buffered, receive)
        await self.app(scope, receive, send)

    async def _buffer(self, receive: Receive) -> list[Message] | None:
        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                return messages
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                return None
            if not message.get("more_body", False):
                return messages

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        error = payload_too_large(self.max_bytes)
        response = error_response(
            Request(scope), error.status_code, error.code, error.message, error.details
        )
        await response(scope, receive, send)


def _replay(messages: list[Message], receive: Receive) -> Receive:
    pending = iter(messages)

    async def replay() -> Message:
        return next(pending, None) or await receive()

    return replay


def create_app(
    settings: Settings | None = None,
    *,
    now: Callable[[], dt.datetime] | None = None,
) -> FastAPI:
    """Build the app. ``now`` must return a timezone-aware datetime (tests inject a fixed one)."""
    resolved = settings or get_settings()
    access_log = _configure_access_log()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        Store.open(resolved.db_path).close()  # create and migrate once; requests only read
        yield

    app = FastAPI(
        title="India Merchant Data API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        responses=ERROR_RESPONSES,
        lifespan=lifespan,
        docs_url="/docs" if resolved.enable_docs else None,
        redoc_url="/redoc" if resolved.enable_docs else None,
        openapi_url="/openapi.json" if resolved.enable_docs else None,
    )
    app.state.settings = resolved
    app.state.now = now or _now_ist
    app.state.snapshots = SnapshotCache(resolved)
    app.add_middleware(BodyLimitMiddleware, max_bytes=resolved.max_request_body_bytes)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = _request_id(request)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            response = internal_error_response(request, exc)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers.update(SECURITY_HEADERS)
        access_log.info(
            json.dumps(
                {
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
                    "route": route_template(request),
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "request_id": request_id,
                }
            )
        )
        return response

    install_error_handlers(app)
    for module in (
        meta,
        offices,
        holidays,
        calendar,
        settlement,
        fx,
        invoice,
        mibor,
        sources,
        webhooks,
        admin,
    ):
        app.include_router(module.router)
    app.include_router(console_router)
    return app
