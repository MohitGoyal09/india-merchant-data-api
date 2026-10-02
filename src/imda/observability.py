"""Light observability helpers shared by the REST and MCP surfaces.

* ``readiness`` backs ``GET /readyz``: cheap read-only checks, short reasons, no paths.
* ``emit_audit`` writes one JSON line per MCP tool call on ``imda.mcp.audit`` (stderr).
* ``route_template`` names the matched route (``/v1/fx/rates``) for the access log.

Nothing here logs argument values, tokens or file paths.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.requests import Request

from imda.models import SourceStatus
from imda.store.repo import Store, StoreUnavailable

AUDIT_LOGGER = "imda.mcp.audit"
AUDIT_EVENT = "mcp_tool_call"
MAX_ARG_KEYS = 20
INVALID_ARG_KEY = "<invalid>"
_ARG_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

Check = dict[str, object]


# ------------------------------------------------------------------------------ readiness
@dataclass(frozen=True, slots=True)
class Readiness:
    ready: bool
    checks: dict[str, Check]

    @property
    def body(self) -> dict[str, object]:
        return {"status": "ready" if self.ready else "not_ready", "checks": self.checks}


def _check(ok: bool, reason: str) -> Check:
    return {"ok": ok, "reason": reason}


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    # ``table`` is always one of the literals below, never caller input.
    return bool(conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0])  # noqa: S608  # nosec B608


def _broken_sources(store: Store) -> list[str]:
    return [
        f"{row['source']}/{row['dataset']}"
        for row in store.source_health()
        if row["status"] == SourceStatus.BROKEN.value
    ]


def _data_checks(store: Store) -> dict[str, Check]:
    conn = store.connection
    has_fx = _exists(conn, "fx_rates")
    has_years = _exists(conn, "holiday_years")
    broken = _broken_sources(store)
    return {
        "fx_rates": _check(has_fx, "loaded" if has_fx else "no FX rates loaded"),
        "holiday_years": _check(has_years, "loaded" if has_years else "no holiday year loaded"),
        "sources": _check(
            not broken, "none broken" if not broken else f"broken: {', '.join(broken)}"
        ),
    }


def _unavailable(reason: str) -> dict[str, Check]:
    skipped = _check(False, "skipped: database unavailable")
    return {
        "database": _check(False, reason),
        "fx_rates": skipped,
        "holiday_years": skipped,
        "sources": skipped,
    }


def readiness(db_path: Path) -> Readiness:
    """Ready only when the DB opens read-only and is migrated, FX and holidays are loaded and
    no source is ``broken``. Never raises; reasons carry no paths or stack traces."""
    try:
        store = Store.open(db_path, read_only=True)
    except StoreUnavailable:
        reason = "database file missing" if not db_path.is_file() else "database not migrated"
        return Readiness(False, _unavailable(reason))
    except sqlite3.Error:
        return Readiness(False, _unavailable("database cannot be opened"))
    try:
        checks = {"database": _check(True, "open read-only and migrated"), **_data_checks(store)}
    except sqlite3.Error:
        return Readiness(False, _unavailable("database query failed"))
    finally:
        store.close()
    return Readiness(all(c["ok"] for c in checks.values()), checks)


# ------------------------------------------------------------------------------ REST access log
def route_template(request: Request) -> str | None:
    """The matched route template (``/v1/items/{item_id}``), or None when nothing matched.

    Valid once the request has been routed, i.e. after ``call_next`` in a middleware.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else None


# ------------------------------------------------------------------------------ MCP audit log
class _StderrHandler(logging.Handler):
    """Writes to whatever ``sys.stderr`` is at emit time. Stdout is the stdio protocol channel."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


def configure_audit_logger() -> logging.Logger:
    """INFO JSON lines to stderr. Idempotent.

    The logger does not propagate: the MCP SDK installs a rich root handler that would wrap
    and re-format the JSON. Attach your own handler to ``imda.mcp.audit`` to ship the lines.
    """
    logger = logging.getLogger(AUDIT_LOGGER)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(h, _StderrHandler) for h in logger.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def safe_arg_keys(arguments: Mapping[str, Any] | None) -> list[str]:
    """Argument NAMES only, sorted and capped. A name that is not an identifier is masked,
    since a hostile client could smuggle a value into a key."""
    keys = sorted(str(key) for key in (arguments or {}))[:MAX_ARG_KEYS]
    return [key if _ARG_KEY.fullmatch(key) else INVALID_ARG_KEY for key in keys]


def emit_audit(
    *,
    tool: str,
    toolset: str | None,
    outcome: str,
    duration_ms: float,
    result_bytes: int,
    truncated: bool,
    arg_keys: Sequence[str],
    request_id: str | None,
    transport: str,
) -> None:
    """One JSON line at INFO on ``imda.mcp.audit``."""
    logging.getLogger(AUDIT_LOGGER).info(
        json.dumps(
            {
                "event": AUDIT_EVENT,
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "tool": tool if _TOOL_NAME.fullmatch(tool) else INVALID_ARG_KEY,
                "toolset": toolset,
                "outcome": outcome,
                "duration_ms": round(duration_ms, 2),
                "result_bytes": result_bytes,
                "truncated": truncated,
                "arg_keys": list(arg_keys),
                "request_id": request_id,
                "transport": transport,
            }
        )
    )
