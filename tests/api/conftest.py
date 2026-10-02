"""API test fixtures: a tmp SQLite DB seeded from the real recorded fixtures."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from imda.api.app import create_app
from imda.config import Settings
from imda.http.client import ExchangeEvent
from imda.models import IST, Dataset, Source
from imda.sources.base import RawPayload, UpstreamRequest
from imda.sources.fbil.fx import FbilFxAdapter
from imda.sources.fbil.mibor import FbilMiborAdapter
from imda.sources.rbi.fx import RbiFxAdapter
from imda.sources.rbi.holidays import RbiHolidayAdapter
from imda.sources.rbi.offices import parse_offices
from imda.store.repo import Store

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FETCHED_AT = dt.datetime(2026, 9, 25, 4, 0, tzinfo=dt.UTC)
# 2026-09-25 15:00 IST: the newest FBIL USD row (2026-09-24) is the last business day, so fresh.
FRESH_NOW = dt.datetime(2026, 9, 25, 15, 0, tzinfo=IST)
# 2026-10-01 15:00 IST: the last business day is 2026-09-30, so FBIL (to 09-24) is stale.
STALE_NOW = dt.datetime(2026, 10, 1, 15, 0, tzinfo=IST)


def _payload(kind: str, name: str, ext: str) -> RawPayload:
    meta = json.loads((FIXTURES / kind / f"{name}.meta.json").read_text(encoding="utf-8"))
    body = (FIXTURES / kind / f"{name}.{ext}").read_bytes()
    request = UpstreamRequest(
        method=meta["method"],
        url=meta["url"],
        params=meta.get("params", {}),
        form=meta.get("form") or None,
    )
    return RawPayload(
        request=request,
        status_code=200,
        body=body,
        content_type="text/plain",
        fetched_at=FETCHED_AT,
        sha256=hashlib.sha256(body).hexdigest(),
        duration_ms=1,
    )


def _log_fetch(store: Store, source: Source, dataset: Dataset, raw: RawPayload) -> str:
    event = ExchangeEvent(
        request=raw.request,
        attempt=1,
        status_code=200,
        bytes=len(raw.body),
        sha256=raw.sha256,
        duration_ms=1,
        fetched_at=FETCHED_AT,
        error=None,
    )
    return store.log_exchange(None, source, dataset, event)


def seed(path: Path) -> None:
    """Offices, Mumbai 2026 + New Delhi 2001 holidays, RBI and FBIL USD..IDR, MIBOR."""
    with Store.open(path) as store:
        page = _payload("rbi", "holidays_page", "html")
        fetch = _log_fetch(store, Source.RBI, Dataset.OFFICES, page)
        store.upsert_offices(parse_offices(page.text()), fetch)

        holidays = RbiHolidayAdapter()
        for name, office, year in (
            ("holidays_mumbai_2026", "mumbai", 2026),
            ("holidays_new_delhi_2001", "new-delhi", 2001),
        ):
            raw = _payload("rbi", name, "html")
            fetch = _log_fetch(store, Source.RBI, Dataset.HOLIDAYS, raw)
            store.replace_holiday_year(office, year, holidays.parse(raw), fetch)

        for name in ("fx_2026_09", "fx_2018_07"):
            raw = _payload("rbi", name, "html")
            fetch = _log_fetch(store, Source.RBI, Dataset.FX, raw)
            store.upsert_fx_rates(RbiFxAdapter().parse(raw), fetch)
        for name in ("fx_2026_09", "fx_2018_07", "fx_2021"):
            raw = _payload("fbil", name, "json")
            fetch = _log_fetch(store, Source.FBIL, Dataset.FX, raw)
            store.upsert_fx_rates(FbilFxAdapter().parse(raw), fetch)

        raw = _payload("fbil", "mibor_2026_09", "json")
        fetch = _log_fetch(store, Source.FBIL, Dataset.MIBOR, raw)
        store.upsert_mibor(FbilMiborAdapter().parse(raw), fetch)


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "imda.sqlite3"
    seed(path)
    return path


@pytest.fixture
def db_path(template_db: Path, tmp_path: Path) -> Path:
    target = tmp_path / "imda.sqlite3"
    shutil.copy(template_db, target)
    return target


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(db_path=db_path, _env_file=None)  # type: ignore[call-arg]


MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client(settings: Settings) -> MakeClient:
    def build(
        now: dt.datetime = FRESH_NOW, custom: Settings | None = None, **kwargs: object
    ) -> TestClient:
        app = create_app(custom or settings, now=lambda: now)
        return TestClient(app, **kwargs)  # type: ignore[arg-type]

    return build


@pytest.fixture
def client(make_client: MakeClient) -> TestClient:
    return make_client()


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store.open(db_path) as opened:
        yield opened
