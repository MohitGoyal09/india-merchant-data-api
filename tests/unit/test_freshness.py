"""Expected publication date and per-dataset staleness."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from imda.config import Settings
from imda.domain.calendar import HolidayCalendar
from imda.health.freshness import (
    FreshnessReport,
    assess_freshness,
    expected_fx_publication,
    expected_latest_fx_date,
)
from imda.http.client import ExchangeEvent
from imda.models import IST, Currency, Dataset, FxRate, Holiday, HolidayKind, Source
from imda.sources.base import UpstreamRequest
from imda.store.repo import Store

CUTOFF = "13:30"
REPUBLIC_DAY = dt.date(2026, 1, 26)  # a Monday


def calendar_for(*years: int, holidays: tuple[Holiday, ...] = ()) -> HolidayCalendar:
    return HolidayCalendar(
        [
            Holiday(
                office_slug="mumbai",
                date=REPUBLIC_DAY,
                name="Republic Day",
                kind=HolidayKind.NI_ACT,
            ),
            *holidays,
        ],
        closing_of_accounts_is_holiday=True,
        loaded_years={"mumbai": frozenset(years)},
    )


def ist(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=IST)


# 2026-10-01 is a Thursday, 2026-10-02 a Friday
@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (ist(2026, 10, 2, 14, 0), dt.date(2026, 10, 2)),  # weekday after the cutoff
        (ist(2026, 10, 2, 13, 30), dt.date(2026, 10, 2)),  # exactly at the cutoff counts
        (ist(2026, 10, 2, 13, 29), dt.date(2026, 10, 1)),  # before the cutoff: yesterday
        (ist(2026, 10, 3, 18, 0), dt.date(2026, 10, 2)),  # Saturday never publishes
        (ist(2026, 10, 4, 18, 0), dt.date(2026, 10, 2)),  # Sunday
        (ist(2026, 10, 5, 10, 0), dt.date(2026, 10, 2)),  # Monday morning: back to Friday
        (ist(2026, 10, 5, 14, 0), dt.date(2026, 10, 5)),  # Monday afternoon
        (ist(2026, 1, 26, 15, 0), dt.date(2026, 1, 23)),  # Republic Day (Mumbai holiday)
        (ist(2026, 1, 27, 10, 0), dt.date(2026, 1, 23)),  # day after, before cutoff
        (ist(2026, 1, 27, 14, 0), dt.date(2026, 1, 27)),
    ],
)
def test_expected_latest_fx_date(now: dt.datetime, expected: dt.date) -> None:
    assert expected_latest_fx_date(now, calendar_for(2026), CUTOFF) == expected


def test_a_first_saturday_is_skipped_although_banks_are_open() -> None:
    # 2026-10-03 is the 1st Saturday: a bank working day, but FX is never published on Saturdays
    assert expected_latest_fx_date(ist(2026, 10, 3, 15), calendar_for(2026), CUTOFF) == dt.date(
        2026, 10, 2
    )


def test_a_utc_clock_is_converted_to_ist() -> None:
    cal = calendar_for(2026)
    after = dt.datetime(2026, 10, 2, 8, 30, tzinfo=dt.UTC)  # 14:00 IST
    before = dt.datetime(2026, 10, 2, 6, 0, tzinfo=dt.UTC)  # 11:30 IST
    assert expected_latest_fx_date(after, cal, CUTOFF) == dt.date(2026, 10, 2)
    assert expected_latest_fx_date(before, cal, CUTOFF) == dt.date(2026, 10, 1)


def test_late_utc_evening_is_already_the_next_ist_day() -> None:
    late = dt.datetime(2026, 10, 1, 20, 0, tzinfo=dt.UTC)  # 01:30 IST on Friday the 2nd
    assert expected_latest_fx_date(late, calendar_for(2026), CUTOFF) == dt.date(2026, 10, 1)


def test_a_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        expected_latest_fx_date(dt.datetime(2026, 10, 2, 14, 0), calendar_for(2026), CUTOFF)


def test_a_bad_cutoff_is_rejected() -> None:
    with pytest.raises(ValueError, match="cutoff"):
        expected_latest_fx_date(ist(2026, 10, 2), calendar_for(2026), "half past one")


def test_complete_calendar_is_not_flagged() -> None:
    result = expected_fx_publication(ist(2026, 10, 2, 14), calendar_for(2026), CUTOFF)
    assert result.date == dt.date(2026, 10, 2)
    assert not result.calendar_incomplete


def test_missing_calendar_falls_back_to_weekends_and_flags_it() -> None:
    result = expected_fx_publication(ist(2026, 1, 26, 15), calendar_for(), CUTOFF)
    assert result.calendar_incomplete
    assert result.date == REPUBLIC_DAY  # the holiday cannot be known without data


def test_weekend_only_walk_needs_no_calendar_until_a_weekday() -> None:
    saturday = expected_fx_publication(ist(2026, 10, 3, 15), calendar_for(), CUTOFF)
    assert saturday.date == dt.date(2026, 10, 2)
    assert saturday.calendar_incomplete  # Friday had to be checked against missing data


def test_a_wrong_year_in_the_calendar_is_flagged_not_raised() -> None:
    result = expected_fx_publication(ist(2026, 10, 2, 14), calendar_for(2025), CUTOFF)
    assert result.date == dt.date(2026, 10, 2)
    assert result.calendar_incomplete


# ---------------------------------------------------------------- assess_freshness
class FakeStore:
    def __init__(
        self,
        fx: dict[Source, dt.date | None] | None = None,
        mibor: dt.date | None = None,
    ) -> None:
        self._fx = fx or {}
        self._mibor = mibor

    def latest_fx_date(self, currency: Currency, source: Source | None = None) -> dt.date | None:
        assert source is not None
        if currency is Currency.USD:
            return self._fx.get(source)
        return None  # other currencies never newer than USD here

    def latest_mibor_date(self) -> dt.date | None:
        return self._mibor


NOW = ist(2026, 10, 2, 14)  # expected latest: Friday 2026-10-02
SETTINGS = Settings(fx_publish_cutoff_ist=CUTOFF)


def by_key(reports: list[FreshnessReport]) -> dict[tuple[Source, Dataset], FreshnessReport]:
    return {(r.source, r.dataset): r for r in reports}


def test_fresh_sources_are_not_stale() -> None:
    today = dt.date(2026, 10, 2)
    store = FakeStore({Source.RBI: today, Source.FBIL: today}, mibor=today)

    reports = assess_freshness(store, calendar_for(2026), NOW, SETTINGS)

    assert [(r.source, r.dataset) for r in reports] == [
        (Source.RBI, Dataset.FX),
        (Source.FBIL, Dataset.FX),
        (Source.FBIL, Dataset.MIBOR),
    ]
    for report in reports:
        assert not report.stale
        assert report.lag_business_days == 0
        assert report.expected_date == today
        assert not report.calendar_incomplete


def test_stale_source_reports_its_lag_in_publication_days() -> None:
    store = FakeStore(
        {Source.RBI: dt.date(2026, 9, 30), Source.FBIL: dt.date(2026, 10, 2)},
        mibor=dt.date(2026, 9, 28),
    )

    reports = by_key(assess_freshness(store, calendar_for(2026), NOW, SETTINGS))

    rbi = reports[(Source.RBI, Dataset.FX)]
    assert rbi.stale
    assert rbi.lag_business_days == 2  # Thu 1st and Fri 2nd are missing
    assert rbi.latest_date == dt.date(2026, 9, 30)
    assert not reports[(Source.FBIL, Dataset.FX)].stale
    mibor = reports[(Source.FBIL, Dataset.MIBOR)]
    assert mibor.stale
    assert mibor.lag_business_days == 4  # Tue, Wed, Thu, Fri


def test_weekend_between_latest_and_expected_is_not_counted_as_lag() -> None:
    now = ist(2026, 10, 5, 14)  # Monday after the cutoff
    store = FakeStore({Source.RBI: dt.date(2026, 10, 2), Source.FBIL: dt.date(2026, 10, 2)})

    rbi = by_key(assess_freshness(store, calendar_for(2026), now, SETTINGS))[
        (Source.RBI, Dataset.FX)
    ]

    assert rbi.lag_business_days == 1
    assert rbi.stale


def test_data_ahead_of_expectation_is_fresh() -> None:
    store = FakeStore({Source.RBI: dt.date(2026, 10, 2)})
    early = ist(2026, 10, 2, 9)  # before the cutoff: expected is Thursday

    rbi = by_key(assess_freshness(store, calendar_for(2026), early, SETTINGS))[
        (Source.RBI, Dataset.FX)
    ]

    assert not rbi.stale
    assert rbi.lag_business_days == 0


def test_a_source_with_no_data_is_stale_with_unknown_lag() -> None:
    reports = assess_freshness(FakeStore(), calendar_for(2026), NOW, SETTINGS)
    for report in reports:
        assert report.stale
        assert report.latest_date is None
        assert report.lag_business_days is None


def test_missing_calendar_is_flagged_on_every_report() -> None:
    reports = assess_freshness(FakeStore(), calendar_for(), NOW, SETTINGS)
    assert all(r.calendar_incomplete for r in reports)


def test_lag_skips_mumbai_holidays() -> None:
    now = ist(2026, 1, 27, 14)  # Tuesday after Republic Day
    store = FakeStore({Source.RBI: dt.date(2026, 1, 23)})

    rbi = by_key(assess_freshness(store, calendar_for(2026), now, SETTINGS))[
        (Source.RBI, Dataset.FX)
    ]

    assert rbi.expected_date == dt.date(2026, 1, 27)
    assert rbi.lag_business_days == 1  # Monday the 26th is a holiday


def test_report_serialises() -> None:
    report = FreshnessReport(
        source=Source.RBI,
        dataset=Dataset.FX,
        latest_date=dt.date(2026, 9, 30),
        expected_date=dt.date(2026, 10, 2),
        lag_business_days=2,
        stale=True,
        calendar_incomplete=False,
    )
    assert report.as_dict() == {
        "source": "rbi",
        "dataset": "fx_reference_rates",
        "latest_date": "2026-09-30",
        "expected_date": "2026-10-02",
        "lag_business_days": 2,
        "stale": True,
        "calendar_incomplete": False,
    }


def test_works_against_the_real_store(tmp_path: Path) -> None:
    with Store.open(tmp_path / "fresh.sqlite3") as store:
        run_id = store.start_run("refresh")
        fetch_id = _log_fetch(store, run_id)
        store.upsert_fx_rates(
            [
                FxRate(
                    currency=Currency.USD,
                    date=dt.date(2026, 10, 1),
                    rate=Decimal("83.5"),
                    unit=1,
                    source=Source.FBIL,
                )
            ],
            fetch_id,
        )
        reports = by_key(assess_freshness(store, calendar_for(2026), NOW, SETTINGS))

    fbil = reports[(Source.FBIL, Dataset.FX)]
    assert fbil.latest_date == dt.date(2026, 10, 1)
    assert fbil.lag_business_days == 1
    assert reports[(Source.RBI, Dataset.FX)].latest_date is None


def _log_fetch(store: Store, run_id: str) -> str:
    event = ExchangeEvent(
        request=UpstreamRequest(method="GET", url="https://example.test/"),
        attempt=1,
        status_code=200,
        bytes=1,
        sha256="0" * 64,
        duration_ms=1,
        fetched_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
        error=None,
    )
    return store.log_exchange(run_id, Source.FBIL, Dataset.FX, event)
