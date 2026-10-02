"""Domain models shared by every layer. All models are immutable."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")


class Source(StrEnum):
    RBI = "rbi"
    FBIL = "fbil"


class Dataset(StrEnum):
    OFFICES = "offices"
    HOLIDAYS = "holidays"
    FX = "fx_reference_rates"
    MIBOR = "mibor_overnight"


class Currency(StrEnum):
    USD = "USD"
    GBP = "GBP"
    EUR = "EUR"
    JPY = "JPY"
    AED = "AED"
    IDR = "IDR"


class HolidayKind(StrEnum):
    NI_ACT = "ni_act"
    """Holiday under the Negotiable Instruments Act: banks closed."""
    CLOSING_OF_ACCOUNTS = "closing_of_accounts"
    """Banks' annual/half-yearly closing of accounts (closed to the public)."""


class SourceStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    BROKEN = "broken"
    UNKNOWN = "unknown"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Office(_Frozen):
    """An RBI regional office. RBI publishes holidays per office, not per state."""

    rbi_id: int = Field(ge=1)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    state: str | None = None


class Holiday(_Frozen):
    office_slug: str
    date: dt.date
    name: str
    kind: HolidayKind


class FxRate(_Frozen):
    """INR value of ``unit`` units of ``currency`` on ``date``.

    Example: ``currency=JPY, unit=100, rate=60.62`` means 100 JPY = 60.62 INR.
    """

    currency: Currency
    date: dt.date
    rate: Decimal = Field(gt=0)
    unit: int = Field(ge=1)
    source: Source
    published_at: dt.datetime | None = None


class MiborRate(_Frozen):
    """FBIL overnight MIBOR, in percent per annum."""

    date: dt.date
    tenor: str
    rate: Decimal
    source: Source = Source.FBIL
    published_at: dt.datetime | None = None


class Provenance(_Frozen):
    source: Source
    dataset: Dataset
    source_url: str
    fetched_at: dt.datetime | None = None
    stale: bool = False


class FetchRecord(_Frozen):
    """One upstream HTTP exchange, kept for audit and provenance."""

    fetch_id: str
    source: Source
    dataset: Dataset
    method: str
    url: str
    params: dict[str, str] = Field(default_factory=dict)
    status_code: int | None = None
    bytes: int = 0
    sha256: str | None = None
    duration_ms: int = 0
    fetched_at: dt.datetime
    error: str | None = None
