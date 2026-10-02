"""SQLite connection and migration."""

from __future__ import annotations

import datetime as dt
import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create) the database at ``path``: WAL, foreign keys on, ``Row`` rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql`` and record the schema version. Safe to call repeatedly."""
    schema = resources.files("imda.store").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, dt.datetime.now(dt.UTC).isoformat()),
        )
