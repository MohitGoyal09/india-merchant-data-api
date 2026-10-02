"""SQLite connection and migration."""

from __future__ import annotations

import datetime as dt
import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


BUSY_TIMEOUT_MS = 5000


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database at ``path``: busy timeout, foreign keys on, ``Row`` rows.

    Read-write (default) creates the file and its parent and switches to WAL. ``read_only``
    opens ``file:...?mode=ro`` with ``query_only`` on: it never creates, migrates or writes.
    The connection is closed again if any setup step fails.
    """
    if read_only:
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


def is_migrated(conn: sqlite3.Connection) -> bool:
    """True when ``schema_version`` records at least the current ``SCHEMA_VERSION``."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:  # no such table
        return False
    return row[0] is not None and row[0] >= SCHEMA_VERSION


def migrate(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql`` and record the schema version. Safe to call repeatedly."""
    schema = resources.files("imda.store").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, dt.datetime.now(dt.UTC).isoformat()),
        )
