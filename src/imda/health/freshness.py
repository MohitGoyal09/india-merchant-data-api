"""Freshness: is each dataset as new as the last Mumbai publication day?

FBIL and RBI publish FX on Mumbai working days after about 13:30 IST, never on a Sunday, and in
practice never on a Saturday. The expected latest date is the newest such day whose cutoff has
passed. When the holiday calendar lacks the year, only weekends are known to be non-working and
the report says so (``calendar_incomplete``), so the caller can lower its confidence.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from imda.config import Settings
from imda.domain.calendar import CalendarDataMissing, HolidayCalendar
from imda.domain.fx_service import FX_CALENDAR_OFFICE
from imda.models import IST, Currency, Dataset, Source

_FIRST_WEEKEND_DAY = 5  # Saturday
_MAX_WALK_DAYS = 31
"""No stretch of Mumbai days without a publication is this long; guards the walk-back loop."""


class FreshnessStore(Protocol):
    """The slice of ``Store`` freshness needs."""

    def latest_fx_date(
        self, currency: Currency, source: Source | None = None
    ) -> dt.date | None: ...

    def latest_mibor_date(self) -> dt.date | None: ...


@dataclass(frozen=True, slots=True)
class ExpectedPublication:
    date: dt.date
    calendar_incomplete: bool


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    source: Source
    dataset: Dataset
    latest_date: dt.date | None
    expected_date: dt.date
    lag_business_days: int | None
    """Publication days between the latest stored date and the expected one; None if no data."""
    stale: bool
    calendar_incomplete: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "dataset": self.dataset.value,
            "latest_date": None if self.latest_date is None else self.latest_date.isoformat(),
            "expected_date": self.expected_date.isoformat(),
            "lag_business_days": self.lag_business_days,
            "stale": self.stale,
            "calendar_incomplete": self.calendar_incomplete,
        }


def _is_publication_day(calendar: HolidayCalendar, day: dt.date) -> tuple[bool, bool]:
    """``(publishes, calendar_missing)``. Weekends never need the calendar."""
    if day.weekday() >= _FIRST_WEEKEND_DAY:
        return False, False
    try:
        return calendar.is_business_day(FX_CALENDAR_OFFICE, day), False
    except CalendarDataMissing:
        return True, True


def expected_fx_publication(
    now: dt.datetime, calendar: HolidayCalendar, cutoff_ist: str
) -> ExpectedPublication:
    """The most recent publication day whose cutoff (``HH:MM`` IST) has passed at ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must carry a timezone")
    try:
        cutoff = dt.time.fromisoformat(cutoff_ist)
    except ValueError as exc:
        raise ValueError(f"invalid cutoff {cutoff_ist!r}: expected HH:MM") from exc
    local = now.astimezone(IST)
    day = local.date() if local.time() >= cutoff else local.date() - dt.timedelta(days=1)
    incomplete = False
    for _ in range(_MAX_WALK_DAYS):
        publishes, missing = _is_publication_day(calendar, day)
        incomplete = incomplete or missing
        if publishes:
            return ExpectedPublication(day, incomplete)
        day -= dt.timedelta(days=1)
    raise ValueError(f"no publication day within {_MAX_WALK_DAYS} days of {local.date()}")


def expected_latest_fx_date(
    now: dt.datetime, calendar: HolidayCalendar, cutoff_ist: str
) -> dt.date:
    return expected_fx_publication(now, calendar, cutoff_ist).date


def _lag(
    calendar: HolidayCalendar, latest: dt.date | None, expected: dt.date
) -> tuple[int | None, bool]:
    """Publication days in ``(latest, expected]``, and whether the calendar was incomplete."""
    if latest is None:
        return None, False
    lag, incomplete = 0, False
    for offset in range(1, (expected - latest).days + 1):
        publishes, missing = _is_publication_day(calendar, latest + dt.timedelta(days=offset))
        incomplete = incomplete or missing
        lag += publishes
    return lag, incomplete


def assess_freshness(
    store: FreshnessStore, calendar: HolidayCalendar, now: dt.datetime, settings: Settings
) -> list[FreshnessReport]:
    """One report each for RBI FX, FBIL FX and FBIL MIBOR."""
    expected = expected_fx_publication(now, calendar, settings.fx_publish_cutoff_ist)
    latest_by_dataset: list[tuple[Source, Dataset, dt.date | None]] = [
        (source, Dataset.FX, _latest_fx(store, source)) for source in (Source.RBI, Source.FBIL)
    ]
    latest_by_dataset.append((Source.FBIL, Dataset.MIBOR, store.latest_mibor_date()))
    reports: list[FreshnessReport] = []
    for source, dataset, latest in latest_by_dataset:
        lag, lag_incomplete = _lag(calendar, latest, expected.date)
        reports.append(
            FreshnessReport(
                source=source,
                dataset=dataset,
                latest_date=latest,
                expected_date=expected.date,
                lag_business_days=lag,
                stale=latest is None or latest < expected.date,
                calendar_incomplete=expected.calendar_incomplete or lag_incomplete,
            )
        )
    return reports


def _latest_fx(store: FreshnessStore, source: Source) -> dt.date | None:
    dates = [store.latest_fx_date(currency, source) for currency in Currency]
    return max((d for d in dates if d is not None), default=None)
