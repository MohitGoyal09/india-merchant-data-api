"""Contracts every source adapter follows.

Flow per dataset::

    adapter.fetch(client, query) -> list[RawPayload]   # may be multi-step (ASP.NET GET then POST)
    adapter.parse(raw)           -> list[Model]         # raises ParseError on unexpected shape
    adapter.fingerprint(raw)     -> dict[str, object]   # stable structural summary for drift checks

Adapters never touch storage. Ingest code owns persistence.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

from imda.models import Dataset, Source

T_co = TypeVar("T_co", covariant=True)
Q_contra = TypeVar("Q_contra", contravariant=True)

HttpMethod = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    method: HttpMethod
    url: str
    params: Mapping[str, str] = field(default_factory=dict)
    form: Mapping[str, str] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawPayload:
    request: UpstreamRequest
    status_code: int
    body: bytes
    content_type: str
    fetched_at: dt.datetime
    sha256: str
    duration_ms: int

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


class UpstreamError(Exception):
    """Network failure, non-2xx after retries, open circuit, budget spent, or upstream disabled."""

    def __init__(self, message: str, *, url: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class ParseError(Exception):
    """Upstream answered, but the payload does not have the shape we expect (likely drift)."""

    def __init__(self, source: Source, dataset: Dataset, reason: str) -> None:
        super().__init__(f"{source}/{dataset}: {reason}")
        self.source = source
        self.dataset = dataset
        self.reason = reason


class HttpClient(Protocol):
    """Implemented by ``imda.http.client.PoliteClient``; fakes implement it in tests."""

    def send(self, request: UpstreamRequest) -> RawPayload:
        """Send one request. Raise ``UpstreamError`` on failure. Never return a non-2xx."""
        ...


@dataclass(frozen=True, slots=True)
class DateRangeQuery:
    start: dt.date
    end: dt.date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"start {self.start} is after end {self.end}")


@dataclass(frozen=True, slots=True)
class HolidayQuery:
    year: int
    month: int | None = None
    """1-12, or None for all months (then ``office_rbi_id`` is required)."""
    office_rbi_id: int | None = None
    """RBI office id, or None for all offices (then ``month`` is required)."""

    def __post_init__(self) -> None:
        if self.month is not None and not 1 <= self.month <= 12:
            raise ValueError(f"month must be 1-12, got {self.month}")
        if self.month is None and self.office_rbi_id is None:
            raise ValueError("RBI rejects all-offices + all-months; set month or office_rbi_id")


class SourceAdapter(Protocol[Q_contra, T_co]):
    source: Source
    dataset: Dataset

    def fetch(self, client: HttpClient, query: Q_contra) -> Sequence[RawPayload]: ...

    def parse(self, raw: RawPayload) -> Sequence[T_co]: ...

    def fingerprint(self, raw: RawPayload) -> dict[str, object]: ...
