"""Live end-to-end ingest. Opt in with ``pytest -m live``.

Real backfill of 2026-09-01..today for offices, FX and MIBOR plus the 2026 holidays into a
temporary database. About 20 requests, paced by ``PoliteClient`` (1 request / 2 s per host).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from imda.config import Settings
from imda.http.client import PoliteClient
from imda.ingest.backfill import backfill
from imda.ingest.common import ExchangeLog
from imda.models import IST, Currency, Dataset, Source
from imda.store.repo import Store

pytestmark = pytest.mark.live

START = dt.date(2026, 9, 1)
MAX_REQUESTS = 40


def test_live_backfill_into_a_temporary_database(tmp_path: Path) -> None:
    today = dt.datetime.now(IST).date()
    settings = Settings(_env_file=None, db_path=tmp_path / "live.sqlite3")
    with Store.open(settings.db_path) as store:
        log = ExchangeLog(store)
        with PoliteClient(settings, on_exchange=log) as client:
            summary = backfill(
                store,
                client,
                start=START,
                end=today,
                datasets={Dataset.OFFICES, Dataset.HOLIDAYS, Dataset.FX, Dataset.MIBOR},
                today=today,
                exchange_log=log,
            )

        failed = {t.key: t.error for t in summary.tasks if t.status == "failed"}
        assert not failed, failed
        assert summary.status == "ok"
        assert summary.requests <= MAX_REQUESTS

        assert len(store.offices()) == 34
        assert store.holidays("mumbai", 2026)
        assert all(2026 in years for years in store.loaded_years().values())
        window = (START, today)
        assert store.fx_rates(Currency.USD, *window, Source.RBI)
        assert store.fx_rates(Currency.USD, *window, Source.FBIL)
        assert store.mibor(*window)
        assert {str(r["status"]) for r in store.source_health()} == {"ok"}
        fetch = store.latest_fetch(Source.FBIL, Dataset.FX)
        assert fetch is not None
        assert "__VIEWSTATE" not in fetch.params
