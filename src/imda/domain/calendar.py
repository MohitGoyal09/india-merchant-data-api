"""Bank-holiday calendar and business-day engine. Pure logic: no I/O.

A day is a non-working day for an RBI regional office when it is a Sunday, the
2nd or 4th Saturday of the month (rule in force from 2015-09-01), or an
RBI-listed holiday for that office (PLAN.md section 4, D6).

The calendar never assumes "no holidays" for a year it has no data for: it raises
``CalendarDataMissing`` instead.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from imda.models import Holiday, HolidayKind

SATURDAY_RULE_START = dt.date(2015, 9, 1)
MAX_ITERATIONS = 366
_SATURDAY = 5
_SUNDAY = 6
_DAYS_PER_WEEK = 7
_ORDINAL_SATURDAY_REASON = {2: "2nd Saturday", 4: "4th Saturday"}
_CLOSING_SUFFIX = " (closing of accounts)"


class CalendarDataMissing(Exception):
    """Raised when RBI holiday data for ``year`` is not loaded for ``office``."""

    def __init__(self, office: str, year: int) -> None:
        self.office = office
        self.year = year
        super().__init__(f"No holiday data loaded for office {office!r}, year {year}")


@dataclass(frozen=True, slots=True)
class SkippedDay:
    date: dt.date
    reason: str


class HolidayCalendar:
    """Immutable per-office calendar built from a set of holidays."""

    __slots__ = ("_closing_is_holiday", "_holidays", "_loaded_years")

    _closing_is_holiday: bool
    _holidays: Mapping[tuple[str, dt.date], tuple[Holiday, ...]]
    _loaded_years: Mapping[str, frozenset[int]]

    def __init__(
        self,
        holidays: Iterable[Holiday],
        *,
        closing_of_accounts_is_holiday: bool,
        loaded_years: Mapping[str, frozenset[int]],
    ) -> None:
        grouped: dict[tuple[str, dt.date], list[Holiday]] = {}
        for holiday in holidays:
            grouped.setdefault((holiday.office_slug, holiday.date), []).append(holiday)
        object.__setattr__(self, "_closing_is_holiday", closing_of_accounts_is_holiday)
        object.__setattr__(
            self,
            "_holidays",
            MappingProxyType({key: tuple(value) for key, value in grouped.items()}),
        )
        object.__setattr__(
            self,
            "_loaded_years",
            MappingProxyType({k: frozenset(v) for k, v in loaded_years.items()}),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("HolidayCalendar is immutable")

    def non_working_reason(self, office: str, day: dt.date) -> str | None:
        """Why ``day`` is not a working day for ``office``; ``None`` if it is one."""
        if day.year not in self._loaded_years.get(office, frozenset()):
            raise CalendarDataMissing(office, day.year)
        reasons = [*_weekend_reasons(day), *self._holiday_reasons(office, day)]
        return "; ".join(reasons) or None

    def is_business_day(self, office: str, day: dt.date) -> bool:
        return self.non_working_reason(office, day) is None

    def next_business_day(
        self, office: str, day: dt.date, *, include_start: bool = False
    ) -> dt.date:
        """First business day after ``day`` (or ``day`` itself when allowed)."""
        current = day if include_start else day + dt.timedelta(days=1)
        for _ in range(MAX_ITERATIONS):
            if self.is_business_day(office, current):
                return current
            current += dt.timedelta(days=1)
        raise ValueError(f"No business day found within {MAX_ITERATIONS} days of {day}")

    def add_business_days(self, office: str, day: dt.date, n: int) -> dt.date:
        """The ``n``-th business day strictly after ``day``.

        ``n == 0`` returns ``day`` if it is a business day, else the next one.
        """
        if n < 0:
            raise ValueError(f"n must be >= 0, got {n}")
        if n == 0:
            return self.next_business_day(office, day, include_start=True)
        current = day
        counted = 0
        for _ in range(MAX_ITERATIONS):
            current += dt.timedelta(days=1)
            if self.is_business_day(office, current):
                counted += 1
                if counted == n:
                    return current
        raise ValueError(f"Could not add {n} business days within {MAX_ITERATIONS} days")

    def business_days_between(self, office: str, start: dt.date, end: dt.date) -> list[dt.date]:
        """Business days in ``[start, end]`` (both ends inclusive)."""
        return [d for d in _iter_days(start, end) if self.is_business_day(office, d)]

    def skipped_days(self, office: str, start: dt.date, end: dt.date) -> list[SkippedDay]:
        """Non-working days in ``[start, end]`` (both ends inclusive), with reasons."""
        pairs = ((d, self.non_working_reason(office, d)) for d in _iter_days(start, end))
        return [SkippedDay(d, reason) for d, reason in pairs if reason is not None]

    def _holiday_reasons(self, office: str, day: dt.date) -> list[str]:
        found = self._holidays.get((office, day), ())
        names = [h.name for h in found if h.kind is HolidayKind.NI_ACT]
        if self._closing_is_holiday:
            names += [
                h.name + _CLOSING_SUFFIX for h in found if h.kind is HolidayKind.CLOSING_OF_ACCOUNTS
            ]
        return list(dict.fromkeys(names))


def _weekend_reasons(day: dt.date) -> list[str]:
    weekday = day.weekday()
    if weekday == _SUNDAY:
        return ["Sunday"]
    if weekday == _SATURDAY and day >= SATURDAY_RULE_START:
        ordinal = (day.day - 1) // _DAYS_PER_WEEK + 1
        reason = _ORDINAL_SATURDAY_REASON.get(ordinal)
        return [reason] if reason else []
    return []


def _iter_days(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    span = (end - start).days + 1
    if span > MAX_ITERATIONS:
        raise ValueError(f"Range of {span} days exceeds the {MAX_ITERATIONS}-day limit")
    return [start + dt.timedelta(days=i) for i in range(span)]
