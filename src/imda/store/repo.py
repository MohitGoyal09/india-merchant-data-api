"""Repository over the SQLite store: one ``Store`` per connection.

Conventions (schema.sql): dates are ISO ``YYYY-MM-DD`` text, timestamps are ISO-8601 with an
offset (UTC here), money and rates are ``Decimal`` stored as text. Write methods run in their
own transaction and return new objects; inputs are never mutated.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from imda.http.client import ExchangeEvent
from imda.models import (
    Currency,
    Dataset,
    FetchRecord,
    FxRate,
    Holiday,
    HolidayKind,
    MiborRate,
    Office,
    Source,
    SourceStatus,
)
from imda.sources.base import UpstreamRequest
from imda.store.db import connect, is_migrated
from imda.store.db import migrate as migrate_schema

RunKind = Literal["backfill", "refresh", "canary"]
RunStatus = Literal["running", "ok", "partial", "failed"]
Row = dict[str, object]


class StoreUnavailable(RuntimeError):
    """The database cannot be opened read-only: the file is missing or not migrated."""


@dataclass(frozen=True, slots=True)
class HolidayDiff:
    """What a replace changed. A renamed holiday appears once in each tuple."""

    added: tuple[Holiday, ...] = ()
    removed: tuple[Holiday, ...] = ()


def exchange_params(request: UpstreamRequest) -> dict[str, str]:
    """Query params plus form fields, minus every ``__*`` field (the ASP.NET viewstate)."""
    form = {k: v for k, v in (request.form or {}).items() if not k.startswith("__")}
    return {**request.params, **form}


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _loads(text: str | None) -> dict[str, object] | None:
    return None if text is None else json.loads(text)


def _dumps(value: dict[str, object] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _year_bounds(year: int) -> tuple[str, str]:
    return f"{year:04d}-01-01", f"{year:04d}-12-31"


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    first = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return first.isoformat(), (nxt - dt.timedelta(days=1)).isoformat()


def _fx_from_row(row: sqlite3.Row) -> FxRate:
    published = row["published_at"]
    return FxRate(
        currency=Currency(row["currency"]),
        date=dt.date.fromisoformat(row["date"]),
        rate=Decimal(row["rate"]),
        unit=row["unit"],
        source=Source(row["source"]),
        published_at=None if published is None else dt.datetime.fromisoformat(published),
    )


def _holiday_from_row(row: sqlite3.Row) -> Holiday:
    return Holiday(
        office_slug=row["office_slug"],
        date=dt.date.fromisoformat(row["date"]),
        name=row["name"],
        kind=HolidayKind(row["kind"]),
    )


class Store:
    """Typed access to the SQLite database. Not shared across connections."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: Path, *, migrate: bool = True, read_only: bool = False) -> Self:
        """Open the database.

        ``read_only`` never creates, migrates or writes (use it for request handling): it
        raises ``StoreUnavailable`` when the file is missing or not migrated. Otherwise the
        schema is applied unless ``migrate`` is false.
        """
        if read_only and not path.is_file():
            raise StoreUnavailable(f"database {path} does not exist")
        conn = connect(path, read_only=read_only)
        try:
            if read_only:
                if not is_migrated(conn):
                    raise StoreUnavailable(f"database {path} is not migrated")
            elif migrate:
                migrate_schema(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on error); re-entrant.

        Taking the write lock up front means a read-then-write method cannot be starved by a
        concurrent writer between its read and its write. A nested call joins the outer one.
        """
        if self._conn.in_transaction:
            yield self._conn
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        """The open connection, for modules that run their own queries (webhooks)."""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---------------------------------------------------------------- runs and fetch log
    def start_run(self, kind: RunKind) -> str:
        run_id = _new_id("run")
        with self._conn:
            self._conn.execute(
                "INSERT INTO ingest_runs (run_id, kind, started_at, status) VALUES (?, ?, ?, ?)",
                (run_id, kind, _now(), "running"),
            )
        return run_id

    def finish_run(self, run_id: str, status: RunStatus, summary: dict[str, object]) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE ingest_runs SET status = ?, finished_at = ?, summary_json = ?"
                " WHERE run_id = ?",
                (status, _now(), _dumps(summary), run_id),
            )

    def runs(self, limit: int = 5) -> list[Row]:
        rows = self._conn.execute(
            "SELECT * FROM ingest_runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "kind": r["kind"],
                "status": r["status"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "summary": _loads(r["summary_json"]),
            }
            for r in rows
        ]

    def log_exchange(
        self, run_id: str | None, source: Source, dataset: Dataset, event: ExchangeEvent
    ) -> str:
        """Record one upstream attempt. Viewstate fields and headers are never stored."""
        fetch_id = _new_id("fetch")
        request = event.request
        with self._conn:
            self._conn.execute(
                "INSERT INTO fetch_log (fetch_id, run_id, source, dataset, method, url,"
                " params_json, status_code, bytes, sha256, duration_ms, fetched_at, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fetch_id,
                    run_id,
                    source.value,
                    dataset.value,
                    request.method,
                    request.url,
                    json.dumps(exchange_params(request), sort_keys=True),
                    event.status_code,
                    event.bytes,
                    event.sha256,
                    event.duration_ms,
                    event.fetched_at.isoformat(),
                    event.error,
                ),
            )
        return fetch_id

    def latest_fetch(self, source: Source, dataset: Dataset) -> FetchRecord | None:
        """The latest successful (2xx, no error) fetch for a dataset."""
        row = self._conn.execute(
            "SELECT * FROM fetch_log WHERE source = ? AND dataset = ? AND error IS NULL"
            " AND status_code BETWEEN 200 AND 299 ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
            (source.value, dataset.value),
        ).fetchone()
        if row is None:
            return None
        return FetchRecord(
            fetch_id=row["fetch_id"],
            source=Source(row["source"]),
            dataset=Dataset(row["dataset"]),
            method=row["method"],
            url=row["url"],
            params=json.loads(row["params_json"]),
            status_code=row["status_code"],
            bytes=row["bytes"],
            sha256=row["sha256"],
            duration_ms=row["duration_ms"],
            fetched_at=dt.datetime.fromisoformat(row["fetched_at"]),
            error=row["error"],
        )

    # ---------------------------------------------------------------- offices and holidays
    def upsert_offices(self, offices: Iterable[Office], fetch_id: str) -> int:
        """Insert new offices and update changed ones. Returns the new or changed count."""
        count = 0
        with self.transaction() as conn:
            for office in offices:
                row = conn.execute(
                    "SELECT rbi_id, name, state FROM offices WHERE slug = ?", (office.slug,)
                ).fetchone()
                if row is not None and tuple(row) == (office.rbi_id, office.name, office.state):
                    continue
                conn.execute(
                    "INSERT INTO offices (slug, rbi_id, name, state, fetch_id)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (slug) DO UPDATE SET"
                    " rbi_id = excluded.rbi_id, name = excluded.name, state = excluded.state,"
                    " fetch_id = excluded.fetch_id",
                    (office.slug, office.rbi_id, office.name, office.state, fetch_id),
                )
                count += 1
        return count

    def offices(self) -> list[Office]:
        rows = self._conn.execute("SELECT * FROM offices ORDER BY slug").fetchall()
        return [
            Office(rbi_id=r["rbi_id"], slug=r["slug"], name=r["name"], state=r["state"])
            for r in rows
        ]

    def replace_holiday_month(
        self,
        office_slug: str,
        year: int,
        month: int,
        holidays: Sequence[Holiday],
        fetch_id: str,
    ) -> HolidayDiff:
        """Replace one office's holidays for one month. Does not mark the year as loaded."""
        low, high = _month_bounds(year, month)
        return self._replace(office_slug, low, high, holidays, fetch_id, mark_year=None)

    def replace_holiday_year(
        self, office_slug: str, year: int, holidays: Sequence[Holiday], fetch_id: str
    ) -> HolidayDiff:
        """Replace one office's holidays for a full year and mark the year as loaded."""
        low, high = _year_bounds(year)
        return self._replace(office_slug, low, high, holidays, fetch_id, mark_year=year)

    def mark_holiday_year_loaded(self, office_slug: str, year: int, fetch_id: str) -> None:
        """Call after all 12 months of ``year`` were replaced successfully."""
        with self.transaction():
            self._mark_year(office_slug, year, fetch_id)

    def _mark_year(self, office_slug: str, year: int, fetch_id: str) -> None:
        self._conn.execute(
            "INSERT INTO holiday_years (office_slug, year, loaded_at, fetch_id)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (office_slug, year) DO UPDATE SET"
            " loaded_at = excluded.loaded_at, fetch_id = excluded.fetch_id",
            (office_slug, year, _now(), fetch_id),
        )

    def replace_holiday_month_all(
        self,
        year: int,
        month: int,
        holidays_by_office: Mapping[str, Sequence[Holiday]],
        fetch_id: str,
    ) -> dict[str, HolidayDiff]:
        """Replace one month for every office in ONE transaction.

        Any failure (unknown office, foreign row, database error) writes nothing for the
        month. ``holidays.updated`` is recorded in the same transaction when anything changed.
        """
        low, high = _month_bounds(year, month)
        with self.transaction():
            diffs = {
                slug: self._replace_in_transaction(slug, low, high, found, fetch_id)
                for slug, found in holidays_by_office.items()
            }
            counts = {
                slug: {"added": len(d.added), "removed": len(d.removed)}
                for slug, d in diffs.items()
                if d.added or d.removed
            }
            if counts:
                period = f"{year:04d}-{month:02d}"
                self.record_event("holidays.updated", {"period": period, "offices": counts})
        return diffs

    def _replace(
        self,
        office_slug: str,
        low: str,
        high: str,
        holidays: Sequence[Holiday],
        fetch_id: str,
        mark_year: int | None,
    ) -> HolidayDiff:
        with self.transaction():
            diff = self._replace_in_transaction(office_slug, low, high, holidays, fetch_id)
            if mark_year is not None:
                self._mark_year(office_slug, mark_year, fetch_id)
        return diff

    def _replace_in_transaction(
        self, office_slug: str, low: str, high: str, holidays: Sequence[Holiday], fetch_id: str
    ) -> HolidayDiff:
        self._require_office(office_slug)
        incoming = _holiday_set(office_slug, low, high, holidays)
        rows = self._conn.execute(
            "SELECT * FROM holidays WHERE office_slug = ? AND date BETWEEN ? AND ?",
            (office_slug, low, high),
        ).fetchall()
        existing = {_holiday_key(h): h for h in map(_holiday_from_row, rows)}
        self._conn.execute(
            "DELETE FROM holidays WHERE office_slug = ? AND date BETWEEN ? AND ?",
            (office_slug, low, high),
        )
        self._conn.executemany(
            "INSERT INTO holidays (office_slug, date, name, kind, fetch_id) VALUES (?, ?, ?, ?, ?)",
            [
                (h.office_slug, h.date.isoformat(), h.name, h.kind.value, fetch_id)
                for h in incoming.values()
            ],
        )
        return HolidayDiff(
            added=tuple(h for h in incoming.values() if existing.get(_holiday_key(h)) != h),
            removed=tuple(h for k, h in existing.items() if incoming.get(k) != h),
        )

    def _require_office(self, office_slug: str) -> None:
        found = self._conn.execute("SELECT 1 FROM offices WHERE slug = ?", (office_slug,))
        if found.fetchone() is None:
            raise ValueError(f"unknown office {office_slug!r}")

    def holidays(self, office_slug: str | None = None, year: int | None = None) -> list[Holiday]:
        clauses: list[str] = []
        args: list[object] = []
        if office_slug is not None:
            clauses.append("office_slug = ?")
            args.append(office_slug)
        if year is not None:
            clauses.append("date BETWEEN ? AND ?")
            args.extend(_year_bounds(year))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            # Fixed fragments only; every value is a bound "?" parameter.
            f"SELECT * FROM holidays{where} ORDER BY office_slug, date, kind",  # noqa: S608  # nosec B608
            args,
        ).fetchall()
        return [_holiday_from_row(r) for r in rows]

    def loaded_years(self) -> dict[str, frozenset[int]]:
        years: dict[str, set[int]] = {}
        for row in self._conn.execute("SELECT office_slug, year FROM holiday_years"):
            years.setdefault(row["office_slug"], set()).add(row["year"])
        return {office: frozenset(found) for office, found in years.items()}

    # ---------------------------------------------------------------- fx
    def upsert_fx_rates(self, rates: Iterable[FxRate], fetch_id: str) -> list[FxRate]:
        """Insert new rows and update changed ones. Returns only the new or changed rates."""
        changed: list[FxRate] = []
        now = _now()
        with self.transaction() as conn:
            for rate in rates:
                if self._fx_unchanged(rate):
                    continue
                conn.execute(
                    "INSERT INTO fx_rates (currency, date, source, rate, unit, published_at,"
                    " fetch_id, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (currency, date, source) DO UPDATE SET rate = excluded.rate,"
                    " unit = excluded.unit, published_at = excluded.published_at,"
                    " fetch_id = excluded.fetch_id, ingested_at = excluded.ingested_at",
                    (
                        rate.currency.value,
                        rate.date.isoformat(),
                        rate.source.value,
                        str(rate.rate),
                        rate.unit,
                        None if rate.published_at is None else rate.published_at.isoformat(),
                        fetch_id,
                        now,
                    ),
                )
                changed.append(rate)
        return changed

    def _fx_unchanged(self, rate: FxRate) -> bool:
        row = self._conn.execute(
            "SELECT rate, unit FROM fx_rates WHERE currency = ? AND date = ? AND source = ?",
            (rate.currency.value, rate.date.isoformat(), rate.source.value),
        ).fetchone()
        return row is not None and row["unit"] == rate.unit and Decimal(row["rate"]) == rate.rate

    def fx_rates(
        self, currency: Currency, start: dt.date, end: dt.date, source: Source | None = None
    ) -> list[FxRate]:
        sql = "SELECT * FROM fx_rates WHERE currency = ? AND date BETWEEN ? AND ?"
        args: list[object] = [currency.value, start.isoformat(), end.isoformat()]
        if source is not None:
            sql += " AND source = ?"
            args.append(source.value)
        rows = self._conn.execute(sql + " ORDER BY date, source", args).fetchall()
        return [_fx_from_row(r) for r in rows]

    def latest_fx_date(self, currency: Currency, source: Source | None = None) -> dt.date | None:
        sql = "SELECT MAX(date) FROM fx_rates WHERE currency = ?"
        args: list[object] = [currency.value]
        if source is not None:
            sql += " AND source = ?"
            args.append(source.value)
        latest = self._conn.execute(sql, args).fetchone()[0]
        return None if latest is None else dt.date.fromisoformat(latest)

    # ---------------------------------------------------------------- mibor
    def upsert_mibor(self, rates: Iterable[MiborRate], fetch_id: str) -> int:
        """Insert new rows and update changed ones. Returns the new or changed count."""
        count = 0
        now = _now()
        with self.transaction() as conn:
            for rate in rates:
                if self._mibor_unchanged(rate):
                    continue
                conn.execute(
                    "INSERT INTO mibor_rates (date, tenor, rate, published_at, fetch_id,"
                    " ingested_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (date, tenor)"
                    " DO UPDATE SET rate = excluded.rate, published_at = excluded.published_at,"
                    " fetch_id = excluded.fetch_id, ingested_at = excluded.ingested_at",
                    (
                        rate.date.isoformat(),
                        rate.tenor,
                        str(rate.rate),
                        None if rate.published_at is None else rate.published_at.isoformat(),
                        fetch_id,
                        now,
                    ),
                )
                count += 1
        return count

    def _mibor_unchanged(self, rate: MiborRate) -> bool:
        row = self._conn.execute(
            "SELECT rate FROM mibor_rates WHERE date = ? AND tenor = ?",
            (rate.date.isoformat(), rate.tenor),
        ).fetchone()
        return row is not None and Decimal(row["rate"]) == rate.rate

    def mibor(self, start: dt.date, end: dt.date, tenor: str | None = None) -> list[MiborRate]:
        sql = "SELECT * FROM mibor_rates WHERE date BETWEEN ? AND ?"
        args: list[object] = [start.isoformat(), end.isoformat()]
        if tenor is not None:
            sql += " AND tenor = ?"
            args.append(tenor)
        rows = self._conn.execute(sql + " ORDER BY date, tenor", args).fetchall()
        return [
            MiborRate(
                date=dt.date.fromisoformat(r["date"]),
                tenor=r["tenor"],
                rate=Decimal(r["rate"]),
                published_at=(
                    None
                    if r["published_at"] is None
                    else dt.datetime.fromisoformat(r["published_at"])
                ),
            )
            for r in rows
        ]

    def latest_mibor_date(self) -> dt.date | None:
        latest = self._conn.execute("SELECT MAX(date) FROM mibor_rates").fetchone()[0]
        return None if latest is None else dt.date.fromisoformat(latest)

    # ---------------------------------------------------------------- health and events
    def set_source_health(
        self,
        source: Source,
        dataset: Dataset,
        status: SourceStatus,
        *,
        error: str | None = None,
        fingerprint: dict[str, object] | None = None,
        drift: dict[str, object] | None = None,
    ) -> SourceStatus | None:
        """Upsert health for a dataset; return the PREVIOUS status (None if never recorded).

        ``ok`` stamps ``last_success_at``; any other status stamps ``last_error_at`` and
        ``last_error``. The other timestamp is kept. Omitted ``fingerprint``/``drift`` keep
        their stored value.
        """
        now = _now()
        with self.transaction() as conn:
            previous = conn.execute(
                "SELECT status FROM source_health WHERE source = ? AND dataset = ?",
                (source.value, dataset.value),
            ).fetchone()
            ok = status is SourceStatus.OK
            conn.execute(
                "INSERT INTO source_health (source, dataset, status, checked_at, last_success_at,"
                " last_error_at, last_error, fingerprint_json, drift_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source, dataset) DO UPDATE SET"
                " status = excluded.status, checked_at = excluded.checked_at,"
                " last_success_at = COALESCE(excluded.last_success_at, last_success_at),"
                " last_error_at = COALESCE(excluded.last_error_at, last_error_at),"
                " last_error = COALESCE(excluded.last_error, last_error),"
                " fingerprint_json = COALESCE(excluded.fingerprint_json, fingerprint_json),"
                " drift_json = COALESCE(excluded.drift_json, drift_json)",
                (
                    source.value,
                    dataset.value,
                    status.value,
                    now,
                    now if ok else None,
                    None if ok else now,
                    None if ok else error,
                    _dumps(fingerprint),
                    _dumps(drift),
                ),
            )
        return None if previous is None else SourceStatus(previous["status"])

    def source_health(self) -> list[Row]:
        rows = self._conn.execute("SELECT * FROM source_health ORDER BY source, dataset")
        return [
            {
                "source": r["source"],
                "dataset": r["dataset"],
                "status": r["status"],
                "checked_at": r["checked_at"],
                "last_success_at": r["last_success_at"],
                "last_error_at": r["last_error_at"],
                "last_error": r["last_error"],
                "fingerprint": _loads(r["fingerprint_json"]),
                "drift": _loads(r["drift_json"]),
            }
            for r in rows.fetchall()
        ]

    def record_event(self, event: str, payload: dict[str, object]) -> str:
        event_id = _new_id("evt")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events (event_id, event, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (event_id, event, _dumps(payload), _now()),
            )
        return event_id

    def events_since(self, created_after: str | None, limit: int = 100) -> list[Row]:
        """Events strictly newer than ``created_after`` (ISO text), oldest first."""
        sql = "SELECT * FROM events"
        args: list[object] = []
        if created_after is not None:
            sql += " WHERE created_at > ?"
            args.append(created_after)
        rows = self._conn.execute(sql + " ORDER BY created_at, rowid LIMIT ?", [*args, limit])
        return [
            {
                "event_id": r["event_id"],
                "event": r["event"],
                "payload": _loads(r["payload_json"]),
                "created_at": r["created_at"],
            }
            for r in rows.fetchall()
        ]


def _holiday_key(holiday: Holiday) -> tuple[str, str]:
    return holiday.date.isoformat(), holiday.kind.value


def _holiday_set(
    office_slug: str, low: str, high: str, holidays: Sequence[Holiday]
) -> dict[tuple[str, str], Holiday]:
    """Index incoming holidays by (date, kind); reject rows for another office or period."""
    incoming: dict[tuple[str, str], Holiday] = {}
    for holiday in holidays:
        if holiday.office_slug != office_slug:
            raise ValueError(
                f"holiday for office {holiday.office_slug!r}, expected {office_slug!r}"
            )
        if not low <= holiday.date.isoformat() <= high:
            raise ValueError(f"holiday {holiday.date} is outside {low}..{high}")
        incoming[_holiday_key(holiday)] = holiday
    return incoming
