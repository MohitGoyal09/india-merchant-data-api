"""Settlement ETA estimation. Pure logic: no I/O.

Razorpay's docs say "T+2 working days" (working days exclude Sundays, 2nd/4th
Saturdays and bank holidays) but their worked example (captured Sat 2019-02-02,
T+2 -> Mon 2019-02-04) adds calendar days and then rolls forward. Both readings
are supported through ``SettlementMode``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from imda.domain.calendar import HolidayCalendar, SkippedDay
from imda.models import IST


class SettlementMode(StrEnum):
    WORKING_DAYS = "working_days"
    """Count N business days after the capture date (matches the docs text)."""
    CALENDAR_THEN_ROLL = "calendar_then_roll"
    """Add N calendar days, then roll forward to a business day (matches the docs example)."""


@dataclass(frozen=True, slots=True)
class SettlementEstimate:
    captured_at: dt.datetime
    capture_date: dt.date
    cycle_days: int
    mode: SettlementMode
    eta_date: dt.date
    counted_days: list[dt.date]
    skipped: list[SkippedDay]


def estimate_settlement(
    captured_at: dt.datetime,
    office: str,
    cycle_days: int,
    calendar: HolidayCalendar,
    mode: SettlementMode = SettlementMode.WORKING_DAYS,
) -> SettlementEstimate:
    """Estimate the settlement date for a payment captured at ``captured_at``.

    ``capture_date`` (T) is the date in IST. In WORKING_DAYS mode the ETA is the
    ``cycle_days``-th business day strictly after T (``cycle_days == 0``: T if it
    is a business day, else the next one); ``counted_days`` lists T+1..T+N.
    In CALENDAR_THEN_ROLL mode the ETA is T + ``cycle_days`` calendar days, rolled
    forward to a business day; ``counted_days`` is empty.

    ``skipped`` lists every non-working day strictly between T and the ETA
    (both ends exclusive: T is the anchor, and the ETA is a business day), with
    the reason. Raises ``ValueError`` for naive datetimes or a negative cycle, and
    ``CalendarDataMissing`` when a touched year has no loaded holiday data.
    """
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    if cycle_days < 0:
        raise ValueError(f"cycle_days must be >= 0, got {cycle_days}")
    capture_date = captured_at.astimezone(IST).date()
    eta = _eta_date(office, capture_date, cycle_days, calendar, mode)
    counted = _counted_days(office, capture_date, eta, cycle_days, calendar, mode)
    one_day = dt.timedelta(days=1)
    skipped = (
        calendar.skipped_days(office, capture_date + one_day, eta - one_day)
        if eta - capture_date > one_day
        else []
    )
    return SettlementEstimate(
        captured_at=captured_at,
        capture_date=capture_date,
        cycle_days=cycle_days,
        mode=mode,
        eta_date=eta,
        counted_days=counted,
        skipped=skipped,
    )


def _eta_date(
    office: str,
    capture_date: dt.date,
    cycle_days: int,
    calendar: HolidayCalendar,
    mode: SettlementMode,
) -> dt.date:
    if mode is SettlementMode.WORKING_DAYS:
        return calendar.add_business_days(office, capture_date, cycle_days)
    if mode is SettlementMode.CALENDAR_THEN_ROLL:
        target = capture_date + dt.timedelta(days=cycle_days)
        return calendar.next_business_day(office, target, include_start=True)
    raise ValueError(f"Unknown settlement mode: {mode!r}")


def _counted_days(
    office: str,
    capture_date: dt.date,
    eta: dt.date,
    cycle_days: int,
    calendar: HolidayCalendar,
    mode: SettlementMode,
) -> list[dt.date]:
    if mode is not SettlementMode.WORKING_DAYS or cycle_days == 0:
        return []
    return calendar.business_days_between(office, capture_date + dt.timedelta(days=1), eta)
