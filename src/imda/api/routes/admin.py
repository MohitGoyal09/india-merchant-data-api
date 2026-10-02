"""Admin actions (admin token): trigger a refresh, run one webhook dispatch pass."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from imda.api.auth import ADMIN_RESPONSES, AdminDeps
from imda.api.deps import Ctx
from imda.api.errors import ApiError
from imda.config import Settings
from imda.events.webhooks import dispatch_pending
from imda.http.client import PoliteClient
from imda.ingest.common import ExchangeLog
from imda.ingest.refresh import refresh
from imda.models import IST
from imda.store.repo import Store

logger = logging.getLogger("imda.admin")

REFRESH_LEASE_SECONDS = 3600.0
"""A refresh that never reported back (a lost background task) frees the gate after this."""

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=AdminDeps, responses=ADMIN_RESPONSES
)


class RefreshGate:
    """Allows one refresh at a time in this process."""

    def __init__(
        self,
        lease_seconds: float = REFRESH_LEASE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lease = lease_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._token = 0
        self._started: float | None = None

    def try_start(self) -> int | None:
        """A token when the caller may start, or ``None`` while another refresh runs."""
        with self._lock:
            now = self._clock()
            if self._started is not None and now - self._started < self._lease:
                return None
            self._token += 1
            self._started = now
            return self._token

    def finish(self, token: int) -> None:
        with self._lock:
            if token == self._token:
                self._started = None


GATE = RefreshGate()


def run_refresh_job(settings: Settings, today: dt.date, token: int) -> None:
    """Background refresh with its own store and upstream client. Never raises."""
    try:
        with Store.open(settings.db_path) as store:
            log = ExchangeLog(store)
            with PoliteClient(settings, on_exchange=log) as client:
                summary = refresh(store, client, today=today, exchange_log=log)
        logger.info("admin refresh run=%s status=%s", summary.run_id, summary.status)
    except Exception:
        logger.exception("admin refresh crashed")
    finally:
        GATE.finish(token)


@router.post("/refresh", status_code=202, summary="Start an incremental refresh in the background")
def trigger_refresh(request: Request, background: BackgroundTasks) -> JSONResponse:
    token = GATE.try_start()
    if token is None:
        raise ApiError(409, "REFRESH_IN_PROGRESS", "A refresh is already running")
    today = request.app.state.now().astimezone(IST).date()
    background.add_task(run_refresh_job, request.app.state.settings, today, token)
    return JSONResponse(status_code=202, content={"status": "accepted"})


@router.post("/webhooks/dispatch", summary="Run one webhook delivery pass now")
def dispatch_webhooks(ctx: Ctx) -> JSONResponse:
    report = dispatch_pending(ctx.store.connection, ctx.settings)
    return JSONResponse(content=asdict(report))
