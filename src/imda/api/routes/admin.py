"""Admin actions (admin token): start a refresh, start one webhook dispatch pass.

Both return 202 at once and run in a background task. Each has a one-at-a-time gate that lives
on ``app.state``; refreshes also honour ``settings.refresh_cooldown_seconds``.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from imda.api.auth import ADMIN_RESPONSES, AdminDeps
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


class CooldownActive(Exception):
    """A refresh was accepted too recently; ``retry_after`` is the wait in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry in {retry_after:.0f}s")
        self.retry_after = retry_after


class RefreshGate:
    """Allows one run at a time in this process, with an optional cooldown between starts."""

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
        self._last_accepted: float | None = None

    def try_start(self, cooldown: float = 0.0) -> int | None:
        """A token when the caller may start, or ``None`` while another run is active.

        Raises ``CooldownActive`` when idle but the last accepted start was less than
        ``cooldown`` seconds ago.
        """
        with self._lock:
            now = self._clock()
            if self._started is not None and now - self._started < self._lease:
                return None
            if self._last_accepted is not None and now - self._last_accepted < cooldown:
                raise CooldownActive(cooldown - (now - self._last_accepted))
            self._token += 1
            self._started = self._last_accepted = now
            return self._token

    def finish(self, token: int) -> None:
        with self._lock:
            if token == self._token:
                self._started = None


_GATE_INIT_LOCK = threading.Lock()


def _gate(app: FastAPI, name: str) -> RefreshGate:
    """The app's gate ``name``, created on first use (one per app, never a module global)."""
    with _GATE_INIT_LOCK:
        gate: RefreshGate | None = getattr(app.state, name, None)
        if gate is None:
            gate = RefreshGate()
            setattr(app.state, name, gate)
        return gate


def refresh_gate_for(app: FastAPI) -> RefreshGate:
    return _gate(app, "refresh_gate")


def dispatch_gate_for(app: FastAPI) -> RefreshGate:
    return _gate(app, "dispatch_gate")


def accepted_response() -> JSONResponse:
    """202 in the standard envelope. Nothing is served from stored data, so no provenance."""
    body: dict[str, Any] = {
        "data": {"status": "accepted"},
        "meta": {"count": 1, "next_cursor": None, "degraded": False, "warnings": []},
        "provenance": [],
    }
    return JSONResponse(status_code=202, content=body)


def run_refresh_job(settings: Settings, today: dt.date, gate: RefreshGate, token: int) -> None:
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
        gate.finish(token)


def run_dispatch_job(settings: Settings, gate: RefreshGate, token: int) -> None:
    """Background dispatch pass with its own store. Never raises."""
    try:
        with Store.open(settings.db_path) as store:
            report = dispatch_pending(store.connection, settings)
        logger.info(
            "admin dispatch sent=%d succeeded=%d failed=%d skipped=%d",
            report.sent,
            report.succeeded,
            report.failed,
            report.skipped_unsafe,
        )
    except Exception:
        logger.exception("admin dispatch crashed")
    finally:
        gate.finish(token)


@router.post("/refresh", status_code=202, summary="Start an incremental refresh in the background")
def trigger_refresh(request: Request, background: BackgroundTasks) -> JSONResponse:
    settings: Settings = request.app.state.settings
    gate = refresh_gate_for(request.app)
    try:
        token = gate.try_start(cooldown=settings.refresh_cooldown_seconds)
    except CooldownActive as exc:
        wait = max(1, math.ceil(exc.retry_after))
        raise ApiError(
            429,
            "REFRESH_COOLDOWN",
            f"A refresh was started recently; try again in {wait} seconds",
            {"retry_after_seconds": wait},
            headers={"Retry-After": str(wait)},
        ) from None
    if token is None:
        raise ApiError(409, "REFRESH_IN_PROGRESS", "A refresh is already running")
    today = request.app.state.now().astimezone(IST).date()
    background.add_task(run_refresh_job, settings, today, gate, token)
    return accepted_response()


@router.post(
    "/webhooks/dispatch",
    status_code=202,
    summary="Start one webhook delivery pass in the background",
)
def dispatch_webhooks(request: Request, background: BackgroundTasks) -> JSONResponse:
    gate = dispatch_gate_for(request.app)
    token = gate.try_start()
    if token is None:
        raise ApiError(409, "DISPATCH_IN_PROGRESS", "A dispatch pass is already running")
    background.add_task(run_dispatch_job, request.app.state.settings, gate, token)
    return accepted_response()
