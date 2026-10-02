"""Polite synchronous HTTP client for upstream sources.

Politeness rules (docs/PLAN.md section 7), all enforced here and nowhere else:

- kill switch (``IMDA_UPSTREAM_ENABLED=false``) and a hard per-instance request budget
- at least ``min_interval_seconds`` between the end of one attempt and the start of the next
  to the same host
- an honest User-Agent, no redirects followed, a fixed timeout, a response size cap
- retries with full-jitter exponential backoff on 429, 5xx and any ``httpx`` error (timeouts,
  transport and decoding errors), honouring ``Retry-After`` (capped)
- a per-host circuit breaker

Clock, sleep and RNG are injected so every timing rule is testable without waiting.
The client is not thread-safe: ingest makes one call at a time (decision D2).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Self

import httpx

from imda.config import Settings
from imda.sources.base import RawPayload, UpstreamError, UpstreamRequest

_RETRYABLE_STATUS = frozenset({429, *range(500, 600)})


@dataclass(frozen=True, slots=True)
class ExchangeEvent:
    """One upstream attempt, successful or not. Carries no response body."""

    request: UpstreamRequest
    attempt: int
    status_code: int | None
    bytes: int
    sha256: str | None
    duration_ms: int
    fetched_at: dt.datetime
    error: str | None


@dataclass(frozen=True, slots=True)
class _Failure:
    """A failed attempt that may be retried."""

    message: str
    status_code: int | None
    retry_after: float | None


@dataclass(slots=True)
class _Breaker:
    failures: int = 0
    opened_at: float | None = None


def parse_retry_after(value: str | None, now: dt.datetime) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date), else None."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        return _seconds_until(text, now)
    return max(seconds, 0.0) if math.isfinite(seconds) else None


def _seconds_until(http_date: str, now: dt.datetime) -> float | None:
    try:
        when = parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max((when - now).total_seconds(), 0.0)


class PoliteClient:
    """Implements ``imda.sources.base.HttpClient``."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        on_exchange: Callable[[ExchangeEvent], None] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()  # noqa: S311
        self._on_exchange = on_exchange
        self._budget_left = settings.request_budget
        self._last_attempt: dict[str, float] = {}
        self._breakers: dict[str, _Breaker] = {}
        self._http = httpx.Client(
            transport=transport,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=False,
            timeout=settings.timeout_seconds,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def send(self, request: UpstreamRequest) -> RawPayload:
        """Send ``request``; return a 2xx payload or raise ``UpstreamError``."""
        if not self._settings.upstream_enabled:
            raise UpstreamError("upstream access is disabled", url=request.url)
        host = httpx.URL(request.url).host
        breaker = self._breakers.setdefault(host, _Breaker())
        self._check_circuit(breaker, request)

        last: _Failure | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            self._spend_budget(request)
            self._pace(host)
            outcome = self._attempt(request, attempt)
            if isinstance(outcome, RawPayload):
                breaker.failures = 0
                breaker.opened_at = None
                return outcome
            last = outcome
            if attempt < self._settings.max_attempts:
                self._sleep(self._delay(attempt, outcome.retry_after))
        assert last is not None
        self._record_failure(breaker)
        raise UpstreamError(
            f"giving up after {self._settings.max_attempts} attempts: {last.message}",
            url=request.url,
            status_code=last.status_code,
        )

    # -- guards ---------------------------------------------------------------------------

    def _check_circuit(self, breaker: _Breaker, request: UpstreamRequest) -> None:
        if breaker.opened_at is None:
            return
        if self._clock() - breaker.opened_at < self._settings.breaker_cooldown_seconds:
            raise UpstreamError("circuit open", url=request.url)
        # Cooldown over: half-open. This call is the single trial; a failure re-opens.

    def _record_failure(self, breaker: _Breaker) -> None:
        breaker.failures += 1
        if breaker.failures >= self._settings.breaker_failure_threshold:
            breaker.opened_at = self._clock()

    def _spend_budget(self, request: UpstreamRequest) -> None:
        if self._budget_left <= 0:
            raise UpstreamError("request budget exhausted", url=request.url)
        self._budget_left -= 1

    def _pace(self, host: str) -> None:
        last = self._last_attempt.get(host)
        if last is not None:
            remaining = last + self._settings.min_interval_seconds - self._clock()
            if remaining > 0:
                self._sleep(remaining)

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        cap = self._settings.backoff_max_seconds
        if retry_after is not None:
            return min(retry_after, cap)
        ceiling = min(cap, self._settings.backoff_base_seconds * 2 ** (attempt - 1))
        return self._rng.uniform(0, ceiling)

    # -- one attempt ----------------------------------------------------------------------

    def _attempt(self, request: UpstreamRequest, attempt: int) -> RawPayload | _Failure:
        host = httpx.URL(request.url).host
        started = self._clock()
        try:
            return self._exchange(request, attempt, started)
        except httpx.HTTPError as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._emit(request, attempt, None, b"", started, message)
            return _Failure(message, None, None)
        finally:
            # Pacing counts from the end of an attempt, so a slow response is not "free" time.
            self._last_attempt[host] = self._clock()

    def _exchange(
        self, request: UpstreamRequest, attempt: int, started: float
    ) -> RawPayload | _Failure:
        with self._http.stream(
            request.method,
            request.url,
            params=dict(request.params) or None,
            data=dict(request.form) if request.form is not None else None,
            headers=dict(request.headers),
        ) as response:
            status = response.status_code
            body = self._read_capped(response, request, attempt, started)
        error = None if 200 <= status < 300 else _describe(status)
        self._emit(request, attempt, status, body, started, error)
        if error is None:
            return self._payload(request, status, response, body, started)
        if status in _RETRYABLE_STATUS:
            retry_after = parse_retry_after(
                response.headers.get("retry-after"), dt.datetime.now(dt.UTC)
            )
            return _Failure(error, status, retry_after)
        raise UpstreamError(error, url=request.url, status_code=status)

    def _read_capped(
        self, response: httpx.Response, request: UpstreamRequest, attempt: int, started: float
    ) -> bytes:
        """Read the decoded body; over the cap is a non-retried ``UpstreamError``."""
        cap = self._settings.max_response_bytes
        declared = _content_length(response)
        chunks: list[bytes] = []
        total = 0
        if declared is None or declared <= cap:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > cap:
                    break
                chunks.append(chunk)
            else:
                return b"".join(chunks)
        message = f"response exceeds {cap} bytes"
        self._emit(request, attempt, response.status_code, b"".join(chunks), started, message)
        raise UpstreamError(message, url=request.url, status_code=response.status_code)

    def _payload(
        self,
        request: UpstreamRequest,
        status: int,
        response: httpx.Response,
        body: bytes,
        started: float,
    ) -> RawPayload:
        return RawPayload(
            request=request,
            status_code=status,
            body=body,
            content_type=response.headers.get("content-type", ""),
            fetched_at=dt.datetime.now(dt.UTC),
            sha256=hashlib.sha256(body).hexdigest(),
            duration_ms=self._elapsed_ms(started),
        )

    def _emit(
        self,
        request: UpstreamRequest,
        attempt: int,
        status: int | None,
        body: bytes,
        started: float,
        error: str | None,
    ) -> None:
        if self._on_exchange is None:
            return
        self._on_exchange(
            ExchangeEvent(
                request=request,
                attempt=attempt,
                status_code=status,
                bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest() if status is not None else None,
                duration_ms=self._elapsed_ms(started),
                fetched_at=dt.datetime.now(dt.UTC),
                error=error,
            )
        )

    def _elapsed_ms(self, started: float) -> int:
        return round((self._clock() - started) * 1000)


def _content_length(response: httpx.Response) -> int | None:
    try:
        return int(response.headers["content-length"])
    except (KeyError, ValueError):
        return None


def _describe(status: int) -> str:
    if 300 <= status < 400:
        return f"HTTP {status} redirect not followed"
    return f"HTTP {status}"
