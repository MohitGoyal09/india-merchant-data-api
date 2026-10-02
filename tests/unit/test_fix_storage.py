"""Store hardening: read-only opens, immediate write transactions, atomic month replace."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from imda.http.client import ExchangeEvent
from imda.models import (
    Currency,
    Dataset,
    FxRate,
    Holiday,
    HolidayKind,
    Office,
    Source,
    SourceStatus,
)
from imda.sources.base import UpstreamRequest
from imda.store.db import connect, is_migrated, migrate
from imda.store.repo import HolidayDiff, Store, StoreUnavailable

D = dt.date


def office(slug: str) -> Office:
    return Office(rbi_id=len(slug), slug=slug, name=slug.title(), state="X")


def hol(day: dt.date, slug: str = "mumbai", name: str = "X") -> Holiday:
    return Holiday(office_slug=slug, date=day, name=name, kind=HolidayKind.NI_ACT)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "t.sqlite3"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store.open(db_path) as opened:
        yield opened


@pytest.fixture
def fetch_id(store: Store) -> str:
    run_id = store.start_run("backfill")
    event = ExchangeEvent(
        request=UpstreamRequest(method="GET", url="https://x.test/a"),
        attempt=1,
        status_code=200,
        bytes=1,
        sha256="ab" * 32,
        duration_ms=1,
        fetched_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
        error=None,
    )
    return store.log_exchange(run_id, Source.RBI, Dataset.HOLIDAYS, event)


# ---------------------------------------------------------------- fix 1: read-only
def test_read_only_open_sees_data_while_a_writer_holds_begin_immediate(
    db_path: Path, store: Store, fetch_id: str
) -> None:
    store.upsert_offices([office("mumbai")], fetch_id)
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("DELETE FROM offices")
    try:
        with Store.open(db_path, read_only=True) as reader:
            assert [o.slug for o in reader.offices()] == ["mumbai"]  # snapshot, no lock error
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_read_only_store_cannot_write(db_path: Path, store: Store) -> None:
    with Store.open(db_path, read_only=True) as reader:
        assert reader.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.record_event("x", {})


def test_read_only_open_does_not_create_or_migrate(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "t.sqlite3"
    with pytest.raises(StoreUnavailable, match="does not exist"):
        Store.open(missing, read_only=True)
    assert not missing.parent.exists()


def test_read_only_open_rejects_unmigrated_database(db_path: Path) -> None:
    sqlite3.connect(db_path).close()
    with pytest.raises(StoreUnavailable, match="not migrated"):
        Store.open(db_path, read_only=True)


def test_read_only_open_rejects_database_without_current_schema_version(db_path: Path) -> None:
    conn = connect(db_path)
    migrate(conn)
    conn.execute("DELETE FROM schema_version")
    conn.commit()
    conn.close()
    with pytest.raises(StoreUnavailable, match="not migrated"):
        Store.open(db_path, read_only=True)


def test_is_migrated_true_after_migrate(db_path: Path) -> None:
    conn = connect(db_path)
    assert not is_migrated(conn)
    migrate(conn)
    assert is_migrated(conn)
    conn.close()


def test_read_write_open_sets_busy_timeout(store: Store) -> None:
    assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_read_only_open_sets_busy_timeout(db_path: Path, store: Store) -> None:
    with Store.open(db_path, read_only=True) as reader:
        assert reader.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_open_without_migrate_leaves_schema_alone(db_path: Path) -> None:
    with Store.open(db_path, migrate=False) as bare:
        tables = bare.connection.execute("SELECT name FROM sqlite_master").fetchall()
        assert tables == []


def test_open_closes_the_connection_when_migrate_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[sqlite3.Connection] = []
    real_connect = connect

    def spy(path: Path, **kwargs: bool) -> sqlite3.Connection:
        conn = real_connect(path, **kwargs)
        opened.append(conn)
        return conn

    def boom(_: sqlite3.Connection) -> None:
        raise RuntimeError("migrate failed")

    monkeypatch.setattr("imda.store.repo.connect", spy)
    monkeypatch.setattr("imda.store.repo.migrate_schema", boom)
    with pytest.raises(RuntimeError, match="migrate failed"):
        Store.open(db_path)
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_connect_closes_the_connection_when_a_pragma_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[sqlite3.Connection] = []

    class Flaky(sqlite3.Connection):
        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("pragma failed")
            return super().execute(sql, *args)  # type: ignore[arg-type]

    real = sqlite3.connect

    def factory(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real(*args, factory=Flaky, **kwargs)  # type: ignore[arg-type]
        made.append(conn)
        return conn

    monkeypatch.setattr("imda.store.db.sqlite3.connect", factory)
    with pytest.raises(sqlite3.OperationalError, match="pragma failed"):
        connect(db_path)
    with pytest.raises(sqlite3.ProgrammingError):
        made[0].execute("SELECT 1")


# ---------------------------------------------------------------- fix 2: BEGIN IMMEDIATE
def test_transaction_takes_the_write_lock_up_front(db_path: Path, store: Store) -> None:
    other = sqlite3.connect(db_path, timeout=0.05, isolation_level=None)
    try:
        with store.transaction(), pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")  # free again after commit
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_transaction_rolls_back_on_error(store: Store) -> None:

    def write_then_fail() -> None:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO events (event_id, event, payload_json, created_at)"
                " VALUES ('e', 'x', '{}', 'now')"
            )
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        write_then_fail()
    assert store.events_since(None) == []
    assert not store.connection.in_transaction


def test_transaction_is_reentrant(store: Store) -> None:
    with store.transaction():
        with store.transaction():
            store.record_event("inner", {})
        assert store.connection.in_transaction  # inner exit did not commit
    assert not store.connection.in_transaction
    assert [e["event"] for e in store.events_since(None)] == ["inner"]


def test_read_then_write_methods_use_immediate_transactions(
    db_path: Path, store: Store, fetch_id: str
) -> None:
    """A second writer must be locked out for the whole read-modify-write, not just the write."""
    statements: list[str] = []
    store.connection.set_trace_callback(statements.append)
    store.upsert_offices([office("mumbai")], fetch_id)
    store.set_source_health(Source.RBI, Dataset.FX, SourceStatus.OK)
    store.replace_holiday_month("mumbai", 2026, 3, [hol(D(2026, 3, 3))], fetch_id)
    store.replace_holiday_year("mumbai", 2026, [hol(D(2026, 3, 3))], fetch_id)
    store.mark_holiday_year_loaded("mumbai", 2026, fetch_id)
    rate = FxRate(
        currency=Currency.USD,
        date=D(2026, 9, 24),
        rate=Decimal("1"),
        unit=1,
        source=Source.FBIL,
        published_at=None,
    )
    store.upsert_fx_rates([rate], fetch_id)
    store.upsert_mibor([], fetch_id)
    begins = [s for s in statements if s.startswith("BEGIN")]
    assert begins == ["BEGIN IMMEDIATE"] * 7


# ---------------------------------------------------------------- fix 3: atomic month
def seed_offices(store: Store, fetch_id: str) -> None:
    store.upsert_offices([office("mumbai"), office("delhi")], fetch_id)


def test_replace_month_all_writes_every_office_and_one_event(store: Store, fetch_id: str) -> None:
    seed_offices(store, fetch_id)

    diffs = store.replace_holiday_month_all(
        2026,
        3,
        {
            "mumbai": [hol(D(2026, 3, 3)), hol(D(2026, 3, 19))],
            "delhi": [hol(D(2026, 3, 3), "delhi")],
        },
        fetch_id,
    )

    assert {s: (len(d.added), len(d.removed)) for s, d in diffs.items()} == {
        "mumbai": (2, 0),
        "delhi": (1, 0),
    }
    assert len(store.holidays("mumbai", 2026)) == 2
    [event] = store.events_since(None)
    assert event["event"] == "holidays.updated"
    assert event["payload"] == {
        "period": "2026-03",
        "offices": {"mumbai": {"added": 2, "removed": 0}, "delhi": {"added": 1, "removed": 0}},
    }


def test_replace_month_all_without_changes_records_no_event(store: Store, fetch_id: str) -> None:
    seed_offices(store, fetch_id)
    month = {"mumbai": [hol(D(2026, 3, 3))], "delhi": []}
    store.replace_holiday_month_all(2026, 3, month, fetch_id)

    diffs = store.replace_holiday_month_all(2026, 3, month, fetch_id)

    assert diffs == {"mumbai": HolidayDiff(), "delhi": HolidayDiff()}
    assert len(store.events_since(None)) == 1


def test_replace_month_all_partial_failure_writes_nothing(store: Store, fetch_id: str) -> None:
    seed_offices(store, fetch_id)
    store.replace_holiday_month_all(2026, 3, {"mumbai": [hol(D(2026, 3, 3))]}, fetch_id)
    events_before = store.events_since(None)

    bad = {
        "mumbai": [hol(D(2026, 3, 19), name="NEW")],  # would replace the old row
        "delhi": [hol(D(2026, 4, 1), "delhi")],  # outside the month: ValueError
    }
    with pytest.raises(ValueError, match="outside"):
        store.replace_holiday_month_all(2026, 3, bad, fetch_id)

    assert [h.date for h in store.holidays("mumbai")] == [D(2026, 3, 3)]
    assert store.holidays("delhi") == []
    assert store.events_since(None) == events_before
    assert not store.connection.in_transaction


def test_replace_month_all_unknown_office_writes_nothing(store: Store, fetch_id: str) -> None:
    seed_offices(store, fetch_id)
    with pytest.raises(ValueError, match="unknown office"):
        store.replace_holiday_month_all(
            2026, 3, {"mumbai": [hol(D(2026, 3, 3))], "ghost": []}, fetch_id
        )
    assert store.holidays() == []
