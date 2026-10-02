"""Request wiring: per-request store, cached calendar snapshot, and strict parameter types."""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import BeforeValidator

from imda.api.errors import office_not_found, store_unavailable
from imda.config import Settings
from imda.domain.calendar import HolidayCalendar
from imda.domain.fx_service import INR as INR_CODE
from imda.domain.fx_service import FxService
from imda.models import IST, Currency
from imda.store.repo import Store, StoreUnavailable

CALENDAR_TTL_SECONDS = 60.0
MIN_API_DATE = dt.date(2000, 1, 1)
MAX_API_DATE = dt.date(2100, 12, 31)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMOUNT = re.compile(r"^[+-]?\d{1,18}(\.\d{1,18})?$")
_SPACE_BEFORE_OFFSET = re.compile(r"(?<=\d) (?=\d{2}:\d{2}$)")


def _in_api_bounds(day: dt.date) -> dt.date:
    if not MIN_API_DATE <= day <= MAX_API_DATE:
        raise ValueError(f"date must be between {MIN_API_DATE} and {MAX_API_DATE}")
    return day


def _strict_iso_date(value: Any) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return _in_api_bounds(value)
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise ValueError("invalid ISO date: expected YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid ISO date: not a real calendar date") from None
    return _in_api_bounds(parsed)


def _aware_datetime(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        # An unescaped "+" in a query string arrives as a space: "11:00:00 05:30".
        text = _SPACE_BEFORE_OFFSET.sub("+", value.strip())
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("invalid ISO 8601 datetime") from None
    else:
        raise ValueError("invalid ISO 8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset, e.g. 2026-03-27T11:00:00+05:30")
    return parsed


def _amount(value: Any) -> Decimal:
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value.strip()):
        raise ValueError("amount must be a decimal string such as '100.50'")
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        raise ValueError("amount must be a decimal string such as '100.50'") from None


def _currency(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


def _convert_currency(value: Any) -> Any:
    code = _currency(value)
    if code == INR_CODE or (isinstance(code, str) and code in Currency):
        return code
    allowed = ", ".join([INR_CODE, *(c.value for c in Currency)])
    raise ValueError(f"unsupported currency: use one of {allowed}")


def _slug(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


IsoDate = Annotated[dt.date, BeforeValidator(_strict_iso_date)]
AwareDatetime = Annotated[dt.datetime, BeforeValidator(_aware_datetime)]
AmountText = Annotated[Decimal, BeforeValidator(_amount)]
CurrencyCode = Annotated[Currency, BeforeValidator(_currency)]
ConvertCurrency = Annotated[str, BeforeValidator(_convert_currency)]
OfficeSlug = Annotated[str, BeforeValidator(_slug)]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Holiday data shared by requests for a short time."""

    calendar: HolidayCalendar
    office_slugs: frozenset[str]
    loaded_years: Mapping[str, frozenset[int]]
    built_at: float


class SnapshotCache:
    """Builds the calendar once per TTL so requests do not reload every holiday row."""

    def __init__(
        self,
        settings: Settings,
        *,
        ttl: float = CALENDAR_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._ttl = ttl
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    def get(self, store: Store) -> Snapshot:
        with self._lock:
            current = self._snapshot
            if current is not None and self.monotonic() - current.built_at < self._ttl:
                return current
            self._snapshot = self._build(store)
            return self._snapshot

    def _build(self, store: Store) -> Snapshot:
        loaded_years = store.loaded_years()
        calendar = HolidayCalendar(
            store.holidays(),
            closing_of_accounts_is_holiday=self._settings.closing_of_accounts_is_holiday,
            loaded_years=loaded_years,
        )
        slugs = frozenset(office.slug for office in store.offices())
        return Snapshot(calendar, slugs, loaded_years, self.monotonic())


@dataclass(frozen=True, slots=True)
class RequestContext:
    store: Store
    settings: Settings
    calendar: HolidayCalendar
    office_slugs: frozenset[str]
    loaded_years: Mapping[str, frozenset[int]]
    fx: FxService
    now: Callable[[], dt.datetime]

    def today(self) -> dt.date:
        return self.now().astimezone(IST).date()

    def has_year(self, office: str, year: int) -> bool:
        return year in self.loaded_years.get(office, frozenset())

    def require_office(self, slug: str) -> str:
        if slug not in self.office_slugs:
            raise office_not_found(slug)
        return slug


def get_context(request: Request) -> Iterator[RequestContext]:
    state = request.app.state
    settings: Settings = state.settings
    now: Callable[[], dt.datetime] = state.now
    try:
        store = Store.open(settings.db_path, read_only=True)
    except StoreUnavailable:
        raise store_unavailable() from None
    try:
        snapshot = state.snapshots.get(store)
        fx = FxService(store, calendar=snapshot.calendar, settings=settings, now=now)
        yield RequestContext(
            store=store,
            settings=settings,
            calendar=snapshot.calendar,
            office_slugs=snapshot.office_slugs,
            loaded_years=snapshot.loaded_years,
            fx=fx,
            now=now,
        )
    finally:
        store.close()


Ctx = Annotated[RequestContext, Depends(get_context)]

__all__ = [
    "MAX_API_DATE",
    "MIN_API_DATE",
    "AmountText",
    "AwareDatetime",
    "ConvertCurrency",
    "Ctx",
    "CurrencyCode",
    "IsoDate",
    "OfficeSlug",
    "RequestContext",
    "SnapshotCache",
    "get_context",
]
