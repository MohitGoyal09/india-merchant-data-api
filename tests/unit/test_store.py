"""SQLite store: migration, write side, read side."""

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
    MiborRate,
    Office,
    Source,
    SourceStatus,
)
from imda.sources.base import UpstreamRequest
from imda.store.db import connect, migrate
from imda.store.repo import HolidayDiff, Store, exchange_params

D = dt.date
NOW = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "sub" / "t.sqlite3") as opened:
        yield opened


def office(slug: str = "mumbai", rbi_id: int = 28) -> Office:
    return Office(rbi_id=rbi_id, slug=slug, name=slug.title(), state="Maharashtra")


def hol(
    day: dt.date, name: str = "X", slug: str = "mumbai", kind: HolidayKind = HolidayKind.NI_ACT
):
    return Holiday(office_slug=slug, date=day, name=name, kind=kind)


def fx(
    day: dt.date, rate: str = "95.909900", source: Source = Source.FBIL, unit: int = 1
) -> FxRate:
    return FxRate(
        currency=Currency.USD,
        date=day,
        rate=Decimal(rate),
        unit=unit,
        source=source,
        published_at=dt.datetime(
            2026, 9, 24, 12, 45, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))
        ),
    )


def exchange(request: UpstreamRequest, status: int | None = 200, error: str | None = None):
    return ExchangeEvent(
        request=request,
        attempt=1,
        status_code=status,
        bytes=10,
        sha256="ab" * 32 if status else None,
        duration_ms=5,
        fetched_at=NOW,
        error=error,
    )


@pytest.fixture
def run_id(store: Store) -> str:
    return store.start_run("backfill")


@pytest.fixture
def fetch_id(store: Store, run_id: str) -> str:
    request = UpstreamRequest(method="GET", url="https://x.test/a")
    return store.log_exchange(run_id, Source.FBIL, Dataset.FX, exchange(request))


# ------------------------------------------------------------------ db
def test_connect_creates_parent_dir_and_sets_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "a" / "b" / "db.sqlite3")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    migrate(conn)
    migrate(conn)
    assert conn.execute("SELECT version FROM schema_version").fetchall()[0][0] == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"fx_rates", "holidays", "fetch_log", "events", "source_health"} <= tables
    conn.close()


def test_schema_ships_in_package() -> None:
    from importlib import resources

    assert resources.files("imda.store").joinpath("schema.sql").is_file()


# ------------------------------------------------------------------ runs and fetch log
def test_run_lifecycle(store: Store) -> None:
    run_id = store.start_run("refresh")
    assert store.runs()[0]["status"] == "running"
    store.finish_run(run_id, "partial", {"requests": 3})
    [run] = store.runs()
    assert (run["run_id"], run["kind"], run["status"]) == (run_id, "refresh", "partial")
    assert run["summary"] == {"requests": 3}
    assert run["finished_at"] is not None


def test_exchange_params_drops_dunder_form_fields_and_keeps_others() -> None:
    request = UpstreamRequest(
        method="POST",
        url="https://x.test/p",
        params={"q": "1"},
        form={"__VIEWSTATE": "V" * 100, "__EVENTVALIDATION": "E", "drYear": "2026"},
    )
    assert exchange_params(request) == {"q": "1", "drYear": "2026"}


def test_log_exchange_never_stores_viewstate_or_headers(store: Store, run_id: str) -> None:
    request = UpstreamRequest(
        method="POST",
        url="https://rbi.test/p",
        form={"__VIEWSTATE": "SECRETVIEWSTATE" * 50, "drYear": "2026"},
        headers={"Authorization": "Bearer topsecret"},
    )
    fetch_id = store.log_exchange(run_id, Source.RBI, Dataset.HOLIDAYS, exchange(request))
    dump = "\n".join(str(tuple(row)) for row in store._conn.execute("SELECT * FROM fetch_log"))
    assert "SECRETVIEWSTATE" not in dump
    assert "topsecret" not in dump
    assert "__VIEWSTATE" not in dump
    record = store.latest_fetch(Source.RBI, Dataset.HOLIDAYS)
    assert record is not None
    assert (record.fetch_id, record.params) == (fetch_id, {"drYear": "2026"})


def test_latest_fetch_returns_latest_successful_only(store: Store, run_id: str) -> None:
    ok = UpstreamRequest(method="GET", url="https://x.test/ok")
    bad = UpstreamRequest(method="GET", url="https://x.test/bad")
    first = store.log_exchange(run_id, Source.FBIL, Dataset.FX, exchange(ok))
    store.log_exchange(run_id, Source.FBIL, Dataset.FX, exchange(bad, 500, "HTTP 500"))
    store.log_exchange(run_id, Source.FBIL, Dataset.FX, exchange(bad, None, "timeout"))
    record = store.latest_fetch(Source.FBIL, Dataset.FX)
    assert record is not None
    assert record.fetch_id == first
    assert record.fetched_at == NOW
    assert record.source is Source.FBIL
    assert store.latest_fetch(Source.RBI, Dataset.FX) is None


# ------------------------------------------------------------------ offices and holidays
def test_upsert_offices_is_idempotent_and_updates(store: Store, fetch_id: str) -> None:
    assert store.upsert_offices([office(), office("pune", 99)], fetch_id) == 2
    assert store.upsert_offices([office(), office("pune", 99)], fetch_id) == 0
    renamed = Office(rbi_id=28, slug="mumbai", name="Mumbai", state=None)
    assert store.upsert_offices([renamed], fetch_id) == 1
    offices = store.offices()
    assert [o.slug for o in offices] == ["mumbai", "pune"]
    assert offices[0].name == "Mumbai"
    assert offices[0].state is None


def test_replace_month_diffs_and_replace_semantics(store: Store, fetch_id: str) -> None:
    store.upsert_offices([office()], fetch_id)
    first = store.replace_holiday_month(
        "mumbai", 2026, 3, [hol(D(2026, 3, 3)), hol(D(2026, 3, 19), "Gudi")], fetch_id
    )
    assert isinstance(first, HolidayDiff)
    assert len(first.added) == 2
    assert first.removed == ()

    second = store.replace_holiday_month(
        "mumbai", 2026, 3, [hol(D(2026, 3, 3)), hol(D(2026, 3, 21))], fetch_id
    )
    assert [h.date for h in second.added] == [D(2026, 3, 21)]
    assert [h.date for h in second.removed] == [D(2026, 3, 19)]
    assert [h.date for h in store.holidays("mumbai", 2026)] == [D(2026, 3, 3), D(2026, 3, 21)]

    same = store.replace_holiday_month(
        "mumbai", 2026, 3, [hol(D(2026, 3, 3)), hol(D(2026, 3, 21))], fetch_id
    )
    assert same == HolidayDiff(added=(), removed=())


def test_replace_month_does_not_touch_other_months_or_offices(store: Store, fetch_id: str) -> None:
    store.upsert_offices([office(), office("pune", 99)], fetch_id)
    store.replace_holiday_month("mumbai", 2026, 4, [hol(D(2026, 4, 1))], fetch_id)
    store.replace_holiday_month("pune", 2026, 3, [hol(D(2026, 3, 5), slug="pune")], fetch_id)
    store.replace_holiday_month("mumbai", 2026, 3, [], fetch_id)
    assert len(store.holidays()) == 2
    assert [h.office_slug for h in store.holidays(year=2026)] == ["mumbai", "pune"]
    assert store.holidays("mumbai", 2025) == []


def test_name_change_is_remove_plus_add(store: Store, fetch_id: str) -> None:
    store.upsert_offices([office()], fetch_id)
    store.replace_holiday_month("mumbai", 2026, 3, [hol(D(2026, 3, 3), "Old")], fetch_id)
    diff = store.replace_holiday_month("mumbai", 2026, 3, [hol(D(2026, 3, 3), "New")], fetch_id)
    assert [h.name for h in diff.added] == ["New"]
    assert [h.name for h in diff.removed] == ["Old"]


def test_replace_month_rejects_foreign_rows(store: Store, fetch_id: str) -> None:
    store.upsert_offices([office()], fetch_id)
    with pytest.raises(ValueError, match="outside"):
        store.replace_holiday_month("mumbai", 2026, 3, [hol(D(2026, 4, 1))], fetch_id)
    with pytest.raises(ValueError, match="office"):
        store.replace_holiday_month("mumbai", 2026, 3, [hol(D(2026, 3, 1), slug="pune")], fetch_id)
    with pytest.raises(ValueError, match="unknown office"):
        store.replace_holiday_month("nowhere", 2026, 3, [], fetch_id)


def test_replace_year_marks_loaded_even_when_empty(store: Store, fetch_id: str) -> None:
    store.upsert_offices([office()], fetch_id)
    diff = store.replace_holiday_year(
        "mumbai", 2026, [hol(D(2026, 1, 26)), hol(D(2026, 8, 15))], fetch_id
    )
    assert len(diff.added) == 2
    assert store.loaded_years() == {"mumbai": frozenset({2026})}
    store.replace_holiday_year("mumbai", 2025, [], fetch_id)
    assert store.loaded_years() == {"mumbai": frozenset({2025, 2026})}
    with pytest.raises(ValueError, match="outside"):
        store.replace_holiday_year("mumbai", 2026, [hol(D(2025, 1, 1))], fetch_id)


def test_month_replace_alone_does_not_mark_year_loaded(store: Store, fetch_id: str) -> None:
    store.upsert_offices([office()], fetch_id)
    store.replace_holiday_month("mumbai", 2026, 3, [hol(D(2026, 3, 3))], fetch_id)
    assert store.loaded_years() == {}
    store.mark_holiday_year_loaded("mumbai", 2026, fetch_id)
    store.mark_holiday_year_loaded("mumbai", 2026, fetch_id)
    assert store.loaded_years() == {"mumbai": frozenset({2026})}


# ------------------------------------------------------------------ fx
def test_decimal_round_trips_exactly(store: Store, fetch_id: str) -> None:
    store.upsert_fx_rates([fx(D(2026, 9, 24), "95.909900")], fetch_id)
    [row] = store.fx_rates(Currency.USD, D(2026, 9, 1), D(2026, 9, 30))
    assert row.rate == Decimal("95.909900")
    assert str(row.rate) == "95.909900"
    assert isinstance(row.rate, Decimal)
    assert row.unit == 1
    assert row.published_at == dt.datetime(2026, 9, 24, 7, 15, tzinfo=dt.UTC)
    raw = store._conn.execute("SELECT typeof(rate), rate FROM fx_rates").fetchone()
    assert tuple(raw) == ("text", "95.909900")


def test_upsert_fx_returns_only_new_or_changed(store: Store, fetch_id: str) -> None:
    a, b = fx(D(2026, 9, 23)), fx(D(2026, 9, 24), "96")
    assert store.upsert_fx_rates([a, b], fetch_id) == [a, b]
    assert store.upsert_fx_rates([a, b], fetch_id) == []
    # numerically equal Decimal with different scale is not a change
    assert store.upsert_fx_rates([fx(D(2026, 9, 24), "96.000")], fetch_id) == []
    revised = fx(D(2026, 9, 24), "96.5")
    assert store.upsert_fx_rates([revised], fetch_id) == [revised]
    assert store.fx_rates(Currency.USD, D(2026, 9, 24), D(2026, 9, 24))[0].rate == Decimal("96.5")
    assert store._conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 2


def test_fx_reads_filter_and_order(store: Store, fetch_id: str) -> None:
    store.upsert_fx_rates(
        [
            fx(D(2026, 9, 24), source=Source.FBIL),
            fx(D(2026, 9, 24), "95.8", source=Source.RBI),
            fx(D(2026, 9, 23), source=Source.FBIL),
            fx(D(2026, 10, 5), source=Source.FBIL),
        ],
        fetch_id,
    )
    rows = store.fx_rates(Currency.USD, D(2026, 9, 1), D(2026, 9, 30))
    assert [(r.date, r.source) for r in rows] == [
        (D(2026, 9, 23), Source.FBIL),
        (D(2026, 9, 24), Source.FBIL),
        (D(2026, 9, 24), Source.RBI),
    ]
    only_rbi = store.fx_rates(Currency.USD, D(2026, 9, 1), D(2026, 9, 30), Source.RBI)
    assert [r.source for r in only_rbi] == [Source.RBI]
    assert store.fx_rates(Currency.EUR, D(2026, 9, 1), D(2026, 9, 30)) == []
    assert store.latest_fx_date(Currency.USD) == D(2026, 10, 5)
    assert store.latest_fx_date(Currency.USD, Source.RBI) == D(2026, 9, 24)
    assert store.latest_fx_date(Currency.EUR) is None


def test_upsert_fx_rejects_unknown_fetch_id(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_fx_rates([fx(D(2026, 9, 24))], "nope")


# ------------------------------------------------------------------ mibor
def test_mibor_upsert_counts_and_reads(store: Store, fetch_id: str) -> None:
    rates = [
        MiborRate(date=D(2026, 9, 24), tenor="O/N", rate=Decimal("5.13")),
        MiborRate(date=D(2026, 9, 18), tenor="3D", rate=Decimal("5.100")),
        MiborRate(date=D(2026, 9, 18), tenor="O/N", rate=Decimal("5.2")),
    ]
    assert store.upsert_mibor(rates, fetch_id) == 3
    assert store.upsert_mibor(rates, fetch_id) == 0
    changed = MiborRate(date=D(2026, 9, 24), tenor="O/N", rate=Decimal("5.14"))
    assert store.upsert_mibor([changed], fetch_id) == 1
    rows = store.mibor(D(2026, 9, 1), D(2026, 9, 30))
    assert [(r.date, r.tenor) for r in rows] == [
        (D(2026, 9, 18), "3D"),
        (D(2026, 9, 18), "O/N"),
        (D(2026, 9, 24), "O/N"),
    ]
    assert rows[1].rate == Decimal("5.2")
    assert rows[0].rate == Decimal("5.100")
    assert [r.tenor for r in store.mibor(D(2026, 9, 1), D(2026, 9, 30), "3D")] == ["3D"]
    assert store.latest_mibor_date() == D(2026, 9, 24)


def test_latest_mibor_date_empty(store: Store) -> None:
    assert store.latest_mibor_date() is None


# ------------------------------------------------------------------ health and events
def test_set_source_health_returns_previous_and_tracks_timestamps(store: Store) -> None:
    assert store.set_source_health(Source.FBIL, Dataset.FX, SourceStatus.OK) is None
    prev = store.set_source_health(Source.FBIL, Dataset.FX, SourceStatus.DEGRADED, error="HTTP 500")
    assert prev is SourceStatus.OK
    [row] = store.source_health()
    assert row["status"] == "degraded"
    assert row["last_error"] == "HTTP 500"
    assert row["last_success_at"] is not None
    assert row["last_error_at"] is not None

    last_success = row["last_success_at"]
    prev = store.set_source_health(Source.FBIL, Dataset.FX, SourceStatus.DEGRADED, error="HTTP 503")
    assert prev is SourceStatus.DEGRADED
    [row] = store.source_health()
    assert row["last_success_at"] == last_success
    assert row["last_error"] == "HTTP 503"

    prev = store.set_source_health(
        Source.FBIL, Dataset.FX, SourceStatus.OK, fingerprint={"layout": "x"}, drift={}
    )
    assert prev is SourceStatus.DEGRADED
    [row] = store.source_health()
    assert row["status"] == "ok"
    assert row["last_success_at"] >= last_success
    assert row["last_error"] == "HTTP 503"  # history kept
    assert row["fingerprint"] == {"layout": "x"}
    assert row["drift"] == {}

    store.set_source_health(Source.FBIL, Dataset.FX, SourceStatus.OK)
    [row] = store.source_health()
    assert row["fingerprint"] == {"layout": "x"}  # not overwritten when omitted


def test_source_health_sorted_and_none_fields(store: Store) -> None:
    store.set_source_health(Source.RBI, Dataset.OFFICES, SourceStatus.OK)
    store.set_source_health(Source.FBIL, Dataset.MIBOR, SourceStatus.BROKEN, error="drift")
    rows = store.source_health()
    assert [(r["source"], r["dataset"]) for r in rows] == [
        ("fbil", "mibor_overnight"),
        ("rbi", "offices"),
    ]
    assert rows[1]["fingerprint"] is None
    assert rows[1]["drift"] is None


def test_events_roundtrip_and_pagination(store: Store) -> None:
    ids = [store.record_event("fx.rates.published", {"USD": [f"2026-09-{d}"]}) for d in (23, 24)]
    assert len(set(ids)) == 2
    events = store.events_since(None)
    assert [e["event"] for e in events] == ["fx.rates.published"] * 2
    assert events[0]["payload"] == {"USD": ["2026-09-23"]}
    assert events[0]["event_id"] == ids[0]
    after_first = store.events_since(str(events[0]["created_at"]))
    assert [e["event_id"] for e in after_first] == [ids[1]]
    assert len(store.events_since(None, limit=1)) == 1


def test_runs_most_recent_first(store: Store) -> None:
    first = store.start_run("backfill")
    second = store.start_run("refresh")
    assert [r["run_id"] for r in store.runs(limit=5)] == [second, first]
    assert len(store.runs(limit=1)) == 1


def test_store_context_manager_closes(tmp_path: Path) -> None:
    with Store.open(tmp_path / "x.sqlite3") as opened:
        opened.record_event("e", {})
    with pytest.raises(sqlite3.ProgrammingError):
        opened.record_event("e", {})
