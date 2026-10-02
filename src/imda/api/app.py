"""FastAPI application factory. Run with ``uvicorn --factory imda.api.app:create_app``."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from imda import __version__
from imda.api.deps import SnapshotCache
from imda.api.errors import ERROR_RESPONSES, install_error_handlers, internal_error_response
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

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_ACCESS_LOG = "imda.api.access"

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


def create_app(
    settings: Settings | None = None,
    *,
    now: Callable[[], dt.datetime] | None = None,
) -> FastAPI:
    """Build the app. ``now`` must return a timezone-aware datetime (tests inject a fixed one)."""
    resolved = settings or get_settings()
    access_log = _configure_access_log()
    app = FastAPI(
        title="India Merchant Data API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        responses=ERROR_RESPONSES,
    )
    app.state.settings = resolved
    app.state.now = now or _now_ist
    app.state.snapshots = SnapshotCache(resolved)

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
        access_log.info(
            json.dumps(
                {
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
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
    return app
