"""Ingest (backfill and refresh) against a fake upstream that serves the recorded fixtures."""

from __future__ import annotations

import calendar
import datetime as dt
import decimal
import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from imda.config import Settings
from imda.http.client import ExchangeEvent, PoliteClient
from imda.ingest.backfill import backfill
from imda.ingest.common import ExchangeLog, RunEnv, Task, TaskOutput, run_plan
from imda.ingest.loaders import rbi_fx_ranges
from imda.ingest.refresh import refresh
from imda.models import Currency, Dataset, Source, SourceStatus
from imda.sources.base import RawPayload, UpstreamError, UpstreamRequest
from imda.store.repo import Store

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TODAY = dt.date(2026, 10, 2)
SEPT = (dt.date(2026, 9, 1), dt.date(2026, 9, 30))
HOLIDAYS_URL = "https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx"
RBI_FX_URL = "https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx"
FBIL_FX_URL = "https://www.fbil.org.in/wasdm/refrates/fetchfiltered"
FBIL_MIBOR_URL = "https://www.fbil.org.in/wasdm/ovnmibor/fetchfiltered"
ALL_DATASETS = {Dataset.OFFICES, Dataset.HOLIDAYS, Dataset.FX, Dataset.MIBOR}
# (fixture file, first day, last day) of each recorded response
RBI_FX_FIXTURES = (
    ("fx_2018_07", "2018-07-01", "2018-07-31"),
    ("fx_2026_09", "2026-09-01", "2026-09-30"),
)
FBIL_FX_FIXTURES = (
    ("fx_2018_07", "2018-07-01", "2018-07-31"),
    ("fx_2021", "2021-01-01", "2021-12-31"),
    ("fx_2026_09", "2026-09-01", "2026-09-30"),
)


def read(path: str) -> bytes:
    return (FIXTURES / path).read_bytes()


def _overlap(window: tuple[str, str], fixtures: tuple[tuple[str, str, str], ...]) -> str | None:
    for name, first, last in fixtures:
        if window[0] <= last and window[1] >= first:
            return name
    return None


class FakeUpstream:
    """``HttpClient`` serving recorded fixtures. Unknown requests raise ``UpstreamError``."""

    def __init__(
        self,
        *,
        down: tuple[str, ...] = (),
        garbage: dict[str, bytes] | None = None,
        fail_month: int | None = None,
        wrong_month: bool = False,
        extra_years: tuple[int, ...] = (),
        on_exchange: Callable[[ExchangeEvent], None] | None = None,
    ) -> None:
        self.requests: list[UpstreamRequest] = []
        self.down = list(down)
        self.garbage = dict(garbage or {})
        self.fail_month = fail_month
        self.wrong_month = wrong_month
        self.extra_years = extra_years
        self._on_exchange = on_exchange

    def attach(self, log: ExchangeLog) -> None:
        self._on_exchange = log

    def send(self, request: UpstreamRequest) -> RawPayload:
        self.requests.append(request)
        if any(part in request.url for part in self.down):
            self._emit(request, None, b"", "connection refused")
            raise UpstreamError("connection refused", url=request.url)
        body = self._garbage(request) or self.route(request)
        self._emit(request, 200, body, None)
        return RawPayload(
            request=request,
            status_code=200,
            body=body,
            content_type="text/html",
            fetched_at=dt.datetime.now(dt.UTC),
            sha256=hashlib.sha256(body).hexdigest(),
            duration_ms=1,
        )

    def _garbage(self, request: UpstreamRequest) -> bytes | None:
        return next((b for part, b in self.garbage.items() if part in request.url), None)

    def _emit(self, request: UpstreamRequest, status: int | None, body: bytes, error: str | None):
        if self._on_exchange is None:
            return
        self._on_exchange(
            ExchangeEvent(
                request=request,
                attempt=1,
                status_code=status,
                bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest() if status else None,
                duration_ms=1,
                fetched_at=dt.datetime.now(dt.UTC),
                error=error,
            )
        )

    def route(self, request: UpstreamRequest) -> bytes:
        if request.url == HOLIDAYS_URL:
            return self._holidays(request)
        if request.url == RBI_FX_URL:
            return self._rbi_fx(request)
        if request.url == FBIL_FX_URL:
            return self._fbil(request, FBIL_FX_FIXTURES, "refrates")
        if request.url == FBIL_MIBOR_URL:
            return self._fbil(request, (("mibor_2026_09", "2026-09-01", "2026-09-30"),), "")
        raise UpstreamError("unknown request", url=request.url)

    def _holidays(self, request: UpstreamRequest) -> bytes:
        if request.method == "GET":
            return self._holidays_page()
        form = request.form or {}
        year, month = int(form["drYear"]), int(form["drMonth"])
        assert form["drRegionalOffice"] == "0"
        assert "__VIEWSTATE" in form
        if month == self.fail_month:
            raise UpstreamError("HTTP 503", url=request.url, status_code=503)
        if self.wrong_month:
            return read("rbi/holidays_all_2026_03.html")
        recorded = FIXTURES / "rbi" / f"holidays_all_{year}_{month:02d}.html"
        if recorded.exists():
            return recorded.read_bytes()
        return synthesize_month(year, month)

    def _holidays_page(self) -> bytes:
        """The recorded page, with ``extra_years`` added to the ``drYear`` dropdown."""
        page = read("rbi/holidays_page.html")
        newest = b'<option selected="selected" value="2026">2026</option>'
        assert newest in page
        extra = b"".join(f'<option value="{y}">{y}</option>\n'.encode() for y in self.extra_years)
        return page.replace(newest, extra + newest, 1)

    def _rbi_fx(self, request: UpstreamRequest) -> bytes:
        if request.method == "GET":
            return read("rbi/fx_page.html")
        form = request.form or {}
        window = (_iso(form["txtFromDate"]), _iso(form["txtToDate"]))
        name = _overlap(window, RBI_FX_FIXTURES) or "fx_2019_01_gap"
        return read(f"rbi/{name}.html")

    def _fbil(self, request: UpstreamRequest, fixtures: tuple[tuple[str, str, str], ...], _: str):
        window = (request.params["fromDate"], request.params["toDate"])
        name = _overlap(window, fixtures)
        return read(f"fbil/{name}.json") if name else b"[]"


def synthesize_month(year: int, month: int) -> bytes:
    """A month page for a period with no recording: April 2026 relabelled to ``year-month``."""
    html = read("rbi/holidays_all_2026_04.html").decode()
    html = html.replace("April 2026", f"{calendar.month_name[month]} {year}")
    if calendar.monthrange(year, month)[1] >= 29:
        return html.encode()
    tree = HTMLParser(html)
    for table in tree.css("table"):
        header = [c.text(strip=True) for c in table.css("tr")[0].css("th")]
        if header and header[0].endswith(str(year)) and "29" in header:
            column = header.index("29")
            for row in table.css("tr"):
                row.css("th, td")[column].decompose()
    return str(tree.html).encode()


def _iso(ddmmyyyy: str) -> str:
    day, month, year = ddmmyyyy.split("/")
    return f"{year}-{month}-{day}"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "ingest.sqlite3") as opened:
        yield opened


def count(store: Store, table: str) -> int:
    return int(store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def health(store: Store) -> dict[tuple[str, str], str]:
    return {(str(r["source"]), str(r["dataset"])): str(r["status"]) for r in store.source_health()}


def event_names(store: Store) -> list[str]:
    return [str(e["event"]) for e in store.events_since(None, limit=1000)]


def run_backfill(
    store: Store,
    client: FakeUpstream,
    datasets: set[Dataset] | None = None,
    window: tuple[dt.date, dt.date] = SEPT,
    **kwargs: object,
):
    if "exchange_log" not in kwargs:
        log = ExchangeLog(store)
        client.attach(log)
        kwargs["exchange_log"] = log
    return backfill(
        store,
        client,
        start=window[0],
        end=window[1],
        datasets=datasets or ALL_DATASETS,
        today=TODAY,
        **kwargs,  # type: ignore[arg-type]
    )


def run_refresh(store: Store, client: FakeUpstream, today: dt.date = TODAY):
    log = ExchangeLog(store)
    client.attach(log)
    return refresh(store, client, today=today, exchange_log=log)


# ---------------------------------------------------------------- happy path
def test_backfill_loads_every_dataset_with_provenance(store: Store) -> None:
    client = FakeUpstream()

    summary = run_backfill(store, client)

    assert summary.status == "ok"
    assert {t.key: t.status for t in summary.tasks} == {
        "rbi/offices": "ok",
        "rbi/holidays": "ok",
        "rbi/fx_reference_rates": "ok",
        "fbil/fx_reference_rates": "ok",
        "fbil/mibor_overnight": "ok",
    }
    assert len(store.offices()) == 34
    assert store.loaded_years() == {o.slug: frozenset({2026}) for o in store.offices()}
    assert len(store.holidays("mumbai", 2026)) > 0
    assert {r.source for r in store.fx_rates(Currency.USD, *SEPT)} == {Source.RBI, Source.FBIL}
    assert store.latest_fx_date(Currency.USD, Source.FBIL) == dt.date(2026, 9, 24)
    assert len(store.mibor(*SEPT)) > 0
    assert set(health(store).values()) == {"ok"}
    assert len(health(store)) == 5
    [run] = store.runs()
    assert run["status"] == "ok"
    assert run["kind"] == "backfill"
    assert run["summary"]["requests"] == summary.requests == len(client.requests)  # type: ignore[index]
    # 1 shared holiday page GET + 12 months + RBI fx (GET + POST) + FBIL fx + MIBOR
    assert summary.requests == 1 + 12 + 2 + 1 + 1


def test_holiday_page_is_fetched_once_per_run(store: Store) -> None:
    client = FakeUpstream()
    run_backfill(store, client, {Dataset.OFFICES, Dataset.HOLIDAYS})
    gets = [r for r in client.requests if r.method == "GET" and r.url == HOLIDAYS_URL]
    assert len(gets) == 1


def test_holidays_alone_bootstraps_offices(store: Store) -> None:
    summary = run_backfill(store, FakeUpstream(), {Dataset.HOLIDAYS})
    assert summary.status == "ok"
    assert len(store.offices()) == 34


def test_every_row_links_to_the_exchange_that_produced_it(store: Store) -> None:
    run_backfill(store, FakeUpstream())
    rows = store._conn.execute(
        "SELECT f.source, l.source AS log_source, l.url FROM fx_rates f"
        " JOIN fetch_log l ON l.fetch_id = f.fetch_id"
    ).fetchall()
    assert rows
    assert all(r["source"] == r["log_source"] for r in rows)
    assert {r["url"] for r in rows} == {FBIL_FX_URL, RBI_FX_URL}
    orphans = store._conn.execute(
        "SELECT COUNT(*) FROM holidays h LEFT JOIN fetch_log l ON l.fetch_id = h.fetch_id"
        " WHERE l.fetch_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_viewstate_is_never_stored(store: Store) -> None:
    run_backfill(store, FakeUpstream())
    dump = "".join(str(tuple(r)) for r in store._conn.execute("SELECT * FROM fetch_log"))
    assert "__VIEWSTATE" not in dump
    assert "__EVENTVALIDATION" not in dump
    posts = store._conn.execute(
        "SELECT params_json FROM fetch_log WHERE method = 'POST' AND dataset = 'holidays'"
    ).fetchall()
    assert len(posts) == 12
    assert json.loads(posts[0]["params_json"])["drRegionalOffice"] == "0"


def test_published_events_use_currency_to_dates_payload(store: Store) -> None:
    run_backfill(store, FakeUpstream())
    events = [e for e in store.events_since(None, 1000) if e["event"] == "fx.rates.published"]
    assert events
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    assert set(payload) <= {c.value for c in Currency}
    assert all(d.startswith("2026-09") for dates in payload.values() for d in dates)
    holiday_events = [e for e in store.events_since(None, 1000) if e["event"] == "holidays.updated"]
    assert holiday_events  # one per month that changed, recorded with the month's rows
    periods = [e["payload"]["period"] for e in holiday_events]  # type: ignore[index]
    assert len(set(periods)) == len(periods)
    assert all(p.startswith("2026-") for p in periods)
    added = sum(e["payload"]["offices"]["mumbai"]["added"] for e in holiday_events)  # type: ignore[index]
    assert added > 0
    assert all(
        e["payload"]["offices"].get("mumbai", {}).get("removed", 0) == 0  # type: ignore[index]
        for e in holiday_events
    )


def test_old_rates_do_not_raise_published_events(store: Store) -> None:
    run_backfill(
        store, FakeUpstream(), {Dataset.FX}, window=(dt.date(2018, 7, 1), dt.date(2018, 7, 31))
    )
    assert "fx.rates.published" not in event_names(store)
    assert store.latest_fx_date(Currency.USD, Source.FBIL) is not None


# ---------------------------------------------------------------- idempotency
def test_rerun_is_idempotent(store: Store) -> None:
    client = FakeUpstream()
    run_backfill(store, client)
    tables = ("offices", "holidays", "holiday_years", "fx_rates", "mibor_rates")
    before = {t: count(store, t) for t in tables}
    events_before = event_names(store)
    client.requests.clear()

    summary = run_backfill(store, client)

    assert summary.status == "ok"
    assert {t: count(store, t) for t in tables} == before
    assert event_names(store) == events_before
    by_key = {t.key: t for t in summary.tasks}
    assert by_key["rbi/holidays"].status == "skipped"
    assert by_key["rbi/fx_reference_rates"].rows == 0
    assert by_key["fbil/mibor_overnight"].rows == 0
    assert not [r for r in client.requests if r.method == "POST" and r.url == HOLIDAYS_URL]


def test_force_refetches_loaded_years_without_new_diffs(store: Store) -> None:
    client = FakeUpstream()
    run_backfill(store, client, {Dataset.HOLIDAYS})
    events_before = event_names(store)
    client.requests.clear()

    summary = run_backfill(store, client, {Dataset.HOLIDAYS}, force=True)

    posts = [r for r in client.requests if r.method == "POST"]
    assert len(posts) == 12
    assert summary.status == "ok"
    assert event_names(store) == events_before


def test_replace_semantics_report_changes_on_second_load(store: Store) -> None:
    client = FakeUpstream()
    run_backfill(store, client, {Dataset.HOLIDAYS})
    events_after_first = len(
        [e for e in store.events_since(None, 1000) if e["event"] == "holidays.updated"]
    )
    store._conn.execute(
        "DELETE FROM holidays WHERE office_slug = 'mumbai' AND date LIKE '2026-03-%'"
    )
    store._conn.commit()
    run_backfill(store, client, {Dataset.HOLIDAYS}, force=True)
    assert {h.date.day for h in store.holidays("mumbai", 2026) if h.date.month == 3} == {
        3,
        19,
        21,
        26,
        31,
    }
    updates = [e for e in store.events_since(None, 1000) if e["event"] == "holidays.updated"]
    assert len(updates) == events_after_first + 1
    assert updates[-1]["payload"] == {  # type: ignore[index]
        "period": "2026-03",
        "offices": {"mumbai": {"added": 5, "removed": 0}},
    }


# ---------------------------------------------------------------- holiday year coverage
def test_year_is_marked_loaded_only_when_all_12_months_succeed(store: Store) -> None:
    client = FakeUpstream(fail_month=7)

    summary = run_backfill(store, client, {Dataset.HOLIDAYS})

    assert summary.status == "failed"
    assert store.loaded_years() == {}
    assert {h.date.month for h in store.holidays()} == {1, 2, 3, 4, 5, 6}
    assert health(store)[("rbi", "holidays")] == "degraded"

    client.fail_month = None
    summary = run_backfill(store, client, {Dataset.HOLIDAYS})
    assert summary.status == "ok"
    assert set(store.loaded_years()) == {o.slug for o in store.offices()}


def test_holiday_years_are_limited_to_what_rbi_offers(store: Store) -> None:
    client = FakeUpstream()
    summary = backfill(
        store,
        client,
        start=dt.date(2026, 12, 1),
        end=dt.date(2027, 6, 1),
        datasets={Dataset.HOLIDAYS},
        today=dt.date(2027, 6, 1),
    )
    years = {int((r.form or {})["drYear"]) for r in client.requests if r.form}
    assert years == {2026}
    assert summary.status == "ok"


# ---------------------------------------------------------------- next year's holidays
FUTURE_YEAR = (dt.date(2027, 1, 1), dt.date(2027, 12, 31))


def holiday_posts(client: FakeUpstream) -> list[tuple[int, int]]:
    return [
        (int((r.form or {})["drYear"]), int((r.form or {})["drMonth"]))
        for r in client.requests
        if r.url == HOLIDAYS_URL and r.form
    ]


def test_backfill_loads_next_year_for_holidays_when_rbi_offers_it(store: Store) -> None:
    client = FakeUpstream(extra_years=(2027,))

    summary = run_backfill(store, client, {Dataset.HOLIDAYS}, window=FUTURE_YEAR)

    assert summary.status == "ok"
    assert summary.tasks[0].status == "ok"
    assert holiday_posts(client) == [(2027, month) for month in range(1, 13)]
    assert {y for years in store.loaded_years().values() for y in years} == {2027}
    assert store.holidays("mumbai", 2027)


def test_backfill_next_year_not_offered_is_skipped_with_a_clear_note(store: Store) -> None:
    client = FakeUpstream()

    summary = run_backfill(store, client, {Dataset.HOLIDAYS}, window=FUTURE_YEAR)

    [task] = summary.tasks
    assert task.status == "skipped"
    assert task.note is not None
    assert "2027" in task.note
    assert "not offered" in task.note
    assert "2026" in task.note  # says what RBI does offer
    assert holiday_posts(client) == []
    assert store.loaded_years() == {}


def test_backfill_loads_what_is_offered_and_notes_what_is_not(store: Store) -> None:
    client = FakeUpstream()

    summary = run_backfill(
        store,
        client,
        {Dataset.HOLIDAYS},
        window=(dt.date(2026, 1, 1), dt.date(2028, 12, 31)),
    )

    [task] = summary.tasks
    assert task.status == "ok"
    assert {y for y, _ in holiday_posts(client)} == {2026}
    assert task.note is not None
    assert "2027" in task.note
    assert "2028" in task.note


def test_backfill_caps_fx_and_mibor_at_today_even_when_holidays_go_further(store: Store) -> None:
    client = FakeUpstream(extra_years=(2027,))

    run_backfill(
        store,
        client,
        {Dataset.HOLIDAYS, Dataset.MIBOR},
        window=(dt.date(2026, 9, 1), dt.date(2027, 12, 31)),
    )

    mibor = next(r for r in client.requests if r.url == FBIL_MIBOR_URL)
    assert mibor.params["toDate"] == TODAY.isoformat()
    assert (2027, 12) in holiday_posts(client)


def test_backfill_with_fx_still_rejects_a_start_after_today(store: Store) -> None:
    client = FakeUpstream(extra_years=(2027,))
    with pytest.raises(ValueError, match="after end"):
        run_backfill(store, client, {Dataset.HOLIDAYS, Dataset.FX}, window=FUTURE_YEAR)
    assert client.requests == []


def test_backfill_holidays_still_rejects_start_after_end(store: Store) -> None:
    with pytest.raises(ValueError, match="after end"):
        run_backfill(
            store,
            FakeUpstream(extra_years=(2027,)),
            {Dataset.HOLIDAYS},
            window=(dt.date(2027, 6, 1), dt.date(2027, 1, 1)),
        )


def test_month_for_the_wrong_period_is_a_parse_failure(store: Store) -> None:
    summary = run_backfill(store, FakeUpstream(wrong_month=True), {Dataset.HOLIDAYS})
    assert summary.status == "failed"
    assert "returned for 2026-01" in str(summary.tasks[0].error) or "returned for" in str(
        summary.tasks[0].error
    )
    assert health(store)[("rbi", "holidays")] == "broken"


def test_unknown_office_in_matrix_is_a_parse_failure(store: Store) -> None:
    run_backfill(store, FakeUpstream(), {Dataset.OFFICES})
    store._conn.execute("DELETE FROM offices WHERE slug = 'mumbai'")
    store._conn.commit()
    summary = run_backfill(store, FakeUpstream(), {Dataset.HOLIDAYS})
    assert summary.status == "failed"
    assert "unknown office" in str(summary.tasks[0].error)


# ---------------------------------------------------------------- fx ranges
def test_rbi_gap_is_skipped(store: Store) -> None:
    client = FakeUpstream()

    run_backfill(
        store,
        client,
        {Dataset.FX},
        window=(dt.date(2018, 7, 1), dt.date(2022, 5, 1)),
    )

    rbi_windows = [
        (_iso(r.form["txtFromDate"]), _iso(r.form["txtToDate"]))
        for r in client.requests
        if r.url == RBI_FX_URL and r.form
    ]
    assert rbi_windows == [("2018-07-01", "2018-07-24"), ("2022-04-12", "2022-05-01")]
    fbil_windows = [
        (r.params["fromDate"], r.params["toDate"]) for r in client.requests if r.url == FBIL_FX_URL
    ]
    assert fbil_windows[0][0] == "2018-07-10"
    assert all(w[1] <= "2022-05-01" for w in fbil_windows)
    assert not [
        r
        for r in client.requests
        if r.url == RBI_FX_URL
        and r.form
        and "2018-07-24" < _iso(r.form["txtFromDate"]) < "2022-04-12"
    ]


def test_rbi_fx_ranges_clip_to_eras() -> None:
    d = dt.date
    assert [(r.start, r.end) for r in rbi_fx_ranges(d(1999, 1, 1), d(2026, 10, 2))] == [
        (d(2000, 1, 3), d(2018, 7, 24)),
        (d(2022, 4, 12), d(2026, 10, 2)),
    ]
    assert rbi_fx_ranges(d(2019, 1, 1), d(2021, 12, 31)) == []


def test_fx_from_before_fbil_start_uses_rbi_only_for_old_dates(store: Store) -> None:
    client = FakeUpstream()
    run_backfill(store, client, {Dataset.FX}, window=(dt.date(2018, 7, 1), dt.date(2018, 7, 9)))
    assert not [r for r in client.requests if r.url == FBIL_FX_URL]


def test_mibor_starts_no_earlier_than_2015_07_22(store: Store) -> None:
    client = FakeUpstream()
    run_backfill(store, client, {Dataset.MIBOR}, window=(dt.date(2010, 1, 1), dt.date(2015, 7, 31)))
    [request] = [r for r in client.requests if r.url == FBIL_MIBOR_URL]
    assert request.params["fromDate"] == "2015-07-22"


def test_end_is_capped_at_today_and_arguments_are_validated(store: Store) -> None:
    client = FakeUpstream()
    run_backfill(store, client, {Dataset.MIBOR}, window=(dt.date(2026, 9, 1), dt.date(2030, 1, 1)))
    assert client.requests[0].params["toDate"] == TODAY.isoformat()
    with pytest.raises(ValueError, match="after end"):
        run_backfill(store, client, window=(dt.date(2026, 10, 3), dt.date(2026, 10, 4)))
    with pytest.raises(ValueError, match="no datasets"):
        backfill(store, client, start=TODAY, end=TODAY, datasets=set(), today=TODAY)


def test_nothing_to_fetch_is_a_skip_not_a_failure(store: Store) -> None:
    client = FakeUpstream()
    summary = run_backfill(
        store, client, {Dataset.FX}, window=(dt.date(2019, 1, 1), dt.date(2019, 1, 31))
    )
    # RBI gap and before FBIL's start: nothing in range for RBI, FBIL covers it
    assert [t.status for t in summary.tasks] == ["skipped", "ok"]
    assert summary.status == "ok"
    assert health(store) == {("fbil", "fx_reference_rates"): "ok"}


# ---------------------------------------------------------------- error isolation and health
def test_fbil_down_does_not_stop_rbi(store: Store) -> None:
    summary = run_backfill(store, FakeUpstream(down=("fbil.org.in",)))

    assert summary.status == "partial"
    by_key = {t.key: t for t in summary.tasks}
    assert by_key["fbil/fx_reference_rates"].status == "failed"
    assert by_key["fbil/mibor_overnight"].status == "failed"
    assert "connection refused" in str(by_key["fbil/mibor_overnight"].error)
    assert by_key["rbi/fx_reference_rates"].status == "ok"
    assert len(store.fx_rates(Currency.USD, *SEPT, Source.RBI)) > 0
    assert store.fx_rates(Currency.USD, *SEPT, Source.FBIL) == []
    assert len(store.offices()) == 34
    assert health(store)[("fbil", "fx_reference_rates")] == "degraded"
    assert health(store)[("rbi", "fx_reference_rates")] == "ok"
    assert event_names(store).count("source.degraded") == 2
    [run] = store.runs()
    assert run["status"] == "partial"


def test_everything_down_is_a_failed_run(store: Store) -> None:
    summary = run_backfill(store, FakeUpstream(down=("rbi.org.in", "fbil.org.in")))
    assert summary.status == "failed"
    assert store.runs()[0]["status"] == "failed"
    assert set(health(store).values()) == {"degraded"}


def test_parse_error_marks_source_broken_and_keeps_data(store: Store) -> None:
    run_backfill(store, FakeUpstream(), {Dataset.FX})
    rows_before = count(store, "fx_rates")

    summary = run_backfill(
        store, FakeUpstream(garbage={FBIL_FX_URL: b'{"renamed": 1}'}), {Dataset.FX}
    )

    assert summary.status == "partial"
    failed = next(t for t in summary.tasks if t.status == "failed")
    assert failed.key == "fbil/fx_reference_rates"
    assert "JSON list" in str(failed.error)
    assert health(store)[("fbil", "fx_reference_rates")] == "broken"
    assert count(store, "fx_rates") == rows_before
    degraded = [e for e in store.events_since(None, 100) if e["event"] == "source.degraded"]
    assert len(degraded) == 1
    assert degraded[0]["payload"]["status"] == "broken"


def test_transitions_emit_events_exactly_once(store: Store) -> None:
    ok, down = FakeUpstream(), FakeUpstream(down=("fbil.org.in",))
    only_mibor = {Dataset.MIBOR}

    run_backfill(store, ok, only_mibor)
    assert event_names(store) == []
    run_backfill(store, down, only_mibor)
    run_backfill(store, down, only_mibor)
    assert event_names(store) == ["source.degraded"]
    run_backfill(store, ok, only_mibor)
    run_backfill(store, ok, only_mibor)
    assert event_names(store) == ["source.degraded", "source.recovered"]
    run_backfill(store, down, only_mibor)
    assert event_names(store) == ["source.degraded", "source.recovered", "source.degraded"]
    events = store.events_since(None, 100)
    assert events[0]["payload"]["previous"] == "ok"  # type: ignore[index]
    assert events[1]["payload"]["status"] == "ok"  # type: ignore[index]


def test_degraded_to_broken_is_not_a_new_degraded_event(store: Store) -> None:
    run_backfill(store, FakeUpstream(), {Dataset.MIBOR})
    run_backfill(store, FakeUpstream(down=("fbil.org.in",)), {Dataset.MIBOR})
    run_backfill(store, FakeUpstream(garbage={FBIL_MIBOR_URL: b"not json"}), {Dataset.MIBOR})
    assert health(store)[("fbil", "mibor_overnight")] == "broken"
    assert event_names(store) == ["source.degraded"]


def test_unexpected_exception_finishes_the_run_as_failed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr("imda.ingest.common.execute_task", boom)
    with pytest.raises(RuntimeError, match="bug"):
        run_backfill(store, FakeUpstream(), {Dataset.MIBOR})
    [run] = store.runs()
    assert run["status"] == "failed"
    assert "bug" in str(run["summary"])


@pytest.mark.parametrize(
    "error",
    [KeyError("missing"), decimal.InvalidOperation("bad rate"), RuntimeError("bug")],
    ids=["KeyError", "InvalidOperation", "RuntimeError"],
)
def test_unexpected_task_exception_is_recorded_as_broken_and_other_tasks_still_run(
    store: Store, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(_: RunEnv) -> TaskOutput:
        raise error

    def fine(_: RunEnv) -> TaskOutput:
        return TaskOutput(rows=3)

    plan: list[Task] = [
        (Source.FBIL, Dataset.FX, explode),
        (Source.FBIL, Dataset.MIBOR, fine),
    ]

    summary = run_plan(store, FakeUpstream(), "refresh", plan, today=TODAY)

    failed, ok = summary.tasks
    assert (failed.status, failed.health) == ("failed", SourceStatus.BROKEN)
    assert failed.error == f"{type(error).__name__}: {error}"
    assert (ok.status, ok.rows) == ("ok", 3)
    assert summary.status == "partial"
    assert health(store) == {
        ("fbil", "fx_reference_rates"): "broken",
        ("fbil", "mibor_overnight"): "ok",
    }
    assert store.runs()[0]["status"] == "partial"
    assert any(r.exc_info for r in caplog.records)  # logger.exception kept the traceback


def test_unexpected_exception_message_is_truncated(store: Store) -> None:
    def explode(_: RunEnv) -> TaskOutput:
        raise KeyError("x" * 2000)

    summary = run_plan(
        store, FakeUpstream(), "refresh", [(Source.FBIL, Dataset.FX, explode)], today=TODAY
    )

    assert summary.tasks[0].error is not None
    assert len(summary.tasks[0].error) == 500


# ---------------------------------------------------------------- exchange logging
def test_exchange_log_requires_a_run(store: Store) -> None:
    log = ExchangeLog(store)
    event = ExchangeEvent(
        request=UpstreamRequest(method="GET", url="https://x.test"),
        attempt=1,
        status_code=200,
        bytes=1,
        sha256="0" * 64,
        duration_ms=1,
        fetched_at=dt.datetime.now(dt.UTC),
        error=None,
    )
    with pytest.raises(RuntimeError, match="outside of an ingest run"):
        log(event)


def test_client_attempts_are_logged_through_the_callback(store: Store) -> None:
    log = ExchangeLog(store)
    client = FakeUpstream(on_exchange=log)

    summary = run_backfill(store, client, {Dataset.MIBOR}, exchange_log=log)

    assert count(store, "fetch_log") == 1
    assert summary.requests == 1
    row = store._conn.execute("SELECT run_id, source, dataset FROM fetch_log").fetchone()
    assert tuple(row) == (summary.run_id, "fbil", "mibor_overnight")


def test_failed_attempts_are_logged(store: Store) -> None:
    log = ExchangeLog(store)
    client = FakeUpstream(down=("fbil.org.in",), on_exchange=log)
    run_backfill(store, client, {Dataset.MIBOR}, exchange_log=log)
    row = store._conn.execute("SELECT status_code, error FROM fetch_log").fetchone()
    assert tuple(row) == (None, "connection refused")
    assert store.latest_fetch(Source.FBIL, Dataset.MIBOR) is None


def test_polite_client_retries_are_all_logged_without_viewstate(store: Store) -> None:
    attempts = {"fbil": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "fbil" in request.url.host:
            attempts["fbil"] += 1
            if attempts["fbil"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=read("fbil/mibor_2026_09.json"))
        if request.method == "GET":
            return httpx.Response(200, content=read("rbi/holidays_page.html"))
        return httpx.Response(200, content=read("rbi/holidays_all_2026_03.html"))

    settings = Settings(_env_file=None, min_interval_seconds=0, max_attempts=2)
    log = ExchangeLog(store)
    with PoliteClient(
        settings,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        on_exchange=log,
    ) as client:
        summary = backfill(
            store,
            client,
            start=SEPT[0],
            end=SEPT[1],
            datasets={Dataset.MIBOR},
            today=TODAY,
            exchange_log=log,
        )
    assert summary.status == "ok"
    assert summary.requests == 2
    statuses = [
        r[0] for r in store._conn.execute("SELECT status_code FROM fetch_log ORDER BY rowid")
    ]
    assert statuses == [503, 200]
    assert len(store.mibor(*SEPT)) > 0
    record = store.latest_fetch(Source.FBIL, Dataset.MIBOR)
    assert record is not None
    assert record.status_code == 200


# ---------------------------------------------------------------- refresh
def test_refresh_on_empty_store_loads_everything_recent(store: Store) -> None:
    client = FakeUpstream()

    summary = run_refresh(store, client, TODAY)

    assert summary.status == "ok"
    assert len(store.offices()) == 34
    assert set(store.loaded_years()) == {o.slug for o in store.offices()}
    months = {h.date.month for h in store.holidays("mumbai", 2026)}
    assert {3, 10, 11} <= months
    assert store.latest_fx_date(Currency.USD, Source.FBIL) == dt.date(2026, 9, 24)
    assert store.mibor(*SEPT)
    holiday_posts = [r for r in client.requests if r.url == HOLIDAYS_URL and r.method == "POST"]
    assert len(holiday_posts) == 12  # the year load also covers next month
    assert next(r.method for r in client.requests if r.url == HOLIDAYS_URL) == "GET"
    assert len([r for r in client.requests if r.method == "GET" and r.url == HOLIDAYS_URL]) == 1
    assert store.runs()[0]["kind"] == "refresh"
    fbil = next(r for r in client.requests if r.url == FBIL_FX_URL)
    assert fbil.params["fromDate"] == "2026-09-02"  # empty store: 30 days back


def test_second_refresh_is_idempotent_and_incremental(store: Store) -> None:
    client = FakeUpstream()
    run_refresh(store, client, TODAY)
    events_before = event_names(store)
    tables = {t: count(store, t) for t in ("holidays", "holiday_years", "fx_rates", "mibor_rates")}
    client.requests.clear()

    summary = run_refresh(store, client, TODAY)

    assert summary.status == "ok"
    assert all(t.rows == 0 for t in summary.tasks if t.status == "ok")
    assert event_names(store) == events_before
    assert {t: count(store, t) for t in tables} == tables
    fbil = next(r for r in client.requests if r.url == FBIL_FX_URL)
    assert fbil.params["fromDate"] == "2026-09-17"  # latest 2026-09-24 minus 7 days
    month_posts = [r for r in client.requests if r.url == HOLIDAYS_URL and r.method == "POST"]
    assert sorted(int((r.form or {})["drMonth"]) for r in month_posts) == [10, 11]


def test_refresh_in_december_does_not_ask_for_an_unpublished_year(store: Store) -> None:
    client = FakeUpstream()
    december = dt.date(2026, 12, 15)
    run_refresh(store, client, december)
    posts = [
        (int((r.form or {})["drYear"]), int((r.form or {})["drMonth"]))
        for r in client.requests
        if r.url == HOLIDAYS_URL and r.form
    ]
    assert {y for y, _ in posts} == {2026}
    assert len(posts) == 12


def test_refresh_loads_a_newly_offered_year_once(store: Store) -> None:
    client = FakeUpstream(extra_years=(2027,))

    summary = run_refresh(store, client, TODAY)

    assert summary.status == "ok"
    posts = holiday_posts(client)
    assert sorted(m for y, m in posts if y == 2027) == list(range(1, 13))  # 12 POSTs, once
    assert len([p for p in posts if p[0] == 2026]) == 12
    assert {y for years in store.loaded_years().values() for y in years} == {2026, 2027}
    updates = [
        e["payload"]["period"]  # type: ignore[index]
        for e in store.events_since(None, 1000)
        if e["event"] == "holidays.updated"
    ]
    assert any(str(p).startswith("2027-") for p in updates)

    client.requests.clear()
    second = run_refresh(store, client, TODAY)

    assert second.status == "ok"
    assert sorted(holiday_posts(client)) == [(2026, 10), (2026, 11)]  # 2027 is not fetched again


def test_refresh_retries_a_next_year_that_failed_part_way(store: Store) -> None:
    client = FakeUpstream(extra_years=(2027,), fail_month=7)
    first = run_refresh(store, client, TODAY)
    assert first.status == "partial"
    assert 2027 not in {y for years in store.loaded_years().values() for y in years}

    client.fail_month = None
    client.requests.clear()
    second = run_refresh(store, client, TODAY)

    assert second.status == "ok"
    assert {y for years in store.loaded_years().values() for y in years} == {2026, 2027}
    assert len([p for p in holiday_posts(client) if p[0] == 2027]) == 12


def test_refresh_ignores_offered_years_before_the_current_year(store: Store) -> None:
    client = FakeUpstream()

    run_refresh(store, client, TODAY)

    assert {y for y, _ in holiday_posts(client)} == {2026}


def test_refresh_in_december_with_next_year_offered_loads_the_whole_year(store: Store) -> None:
    client = FakeUpstream(extra_years=(2027,))

    run_refresh(store, client, dt.date(2026, 12, 15))

    posts = holiday_posts(client)
    assert sorted(m for y, m in posts if y == 2027) == list(range(1, 13))
    assert len(posts) == 24  # January 2027 is not fetched a second time


def test_refresh_isolates_failures(store: Store) -> None:
    summary = run_refresh(store, FakeUpstream(down=("fbil.org.in",)))
    assert summary.status == "partial"
    assert health(store)[("fbil", "mibor_overnight")] == "degraded"
    assert len(store.offices()) == 34
    assert store.fx_rates(Currency.USD, *SEPT, Source.RBI)
    assert SourceStatus.DEGRADED.value in health(store).values()


# ---------------------------------------------------------------- skips and odd pages
def test_holiday_years_beyond_rbi_are_skipped_without_requests(store: Store) -> None:
    client = FakeUpstream()
    summary = backfill(
        store,
        client,
        start=dt.date(2027, 1, 1),
        end=dt.date(2027, 3, 1),
        datasets={Dataset.HOLIDAYS},
        today=dt.date(2027, 3, 1),
    )
    assert [t.status for t in summary.tasks] == ["skipped"]
    assert [r.method for r in client.requests] == ["GET"]
    assert health(store) == {}


def test_year_dropdown_without_years_is_a_parse_failure(store: Store) -> None:
    page = read("rbi/holidays_page.html").replace(b'value="20', b'value="xx')
    summary = run_backfill(store, FakeUpstream(garbage={HOLIDAYS_URL: page}), {Dataset.HOLIDAYS})
    assert summary.status == "failed"
    assert "no years" in str(summary.tasks[0].error)


def test_mibor_before_its_start_is_skipped(store: Store) -> None:
    window = (dt.date(2010, 1, 1), dt.date(2015, 7, 1))
    summary = run_backfill(store, FakeUpstream(), {Dataset.MIBOR}, window=window)
    assert [t.status for t in summary.tasks] == ["skipped"]


def test_refresh_far_future_has_nothing_to_load_for_holidays(store: Store) -> None:
    summary = run_refresh(store, FakeUpstream(), dt.date(2028, 1, 15))
    holidays = next(t for t in summary.tasks if t.key == "rbi/holidays")
    assert holidays.status == "skipped"
    assert store.loaded_years() == {}
