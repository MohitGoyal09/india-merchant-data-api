"""PoliteClient behaviour, driven by a scripted transport and a fake clock (no real waiting)."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable

import httpx
import pytest

from imda.config import DEFAULT_USER_AGENT, Settings
from imda.http.client import ExchangeEvent, PoliteClient, parse_retry_after
from imda.sources.base import UpstreamError, UpstreamRequest

URL = "https://example.test/data"
Step = httpx.Response | Exception


class FakeTime:
    """Monotonic clock whose ``sleep`` advances it instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class MaxRng:
    """Stand-in for random.Random: always returns the upper bound, records the bounds."""

    def __init__(self) -> None:
        self.bounds: list[float] = []

    def uniform(self, low: float, high: float) -> float:
        assert low == 0
        self.bounds.append(high)
        return high


class Script:
    """Transport handler that replays a list of responses/exceptions, then repeats the last."""

    def __init__(self, steps: list[Step], ft: FakeTime, *, takes: float = 0.0) -> None:
        self.steps = steps
        self.ft = ft
        self.takes = takes
        self.calls: list[httpx.Request] = []
        self.call_times: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.call_times.append(self.ft.now)
        self.ft.now += self.takes
        step = self.steps[min(len(self.calls), len(self.steps)) - 1]
        if isinstance(step, Exception):
            raise step
        return step


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "min_interval_seconds": 2.0,
        "max_attempts": 4,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 30.0,
        "breaker_failure_threshold": 5,
        "breaker_cooldown_seconds": 600.0,
        "request_budget": 500,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def build(
    steps: list[Step],
    *,
    takes: float = 0.0,
    events: list[ExchangeEvent] | None = None,
    **overrides: object,
) -> tuple[PoliteClient, Script, FakeTime, MaxRng]:
    ft = FakeTime()
    script = Script(steps, ft, takes=takes)
    rng = MaxRng()
    on_exchange: Callable[[ExchangeEvent], None] | None = (
        events.append if events is not None else None
    )
    client = PoliteClient(
        make_settings(**overrides),
        transport=httpx.MockTransport(script),
        clock=ft.clock,
        sleep=ft.sleep,
        rng=rng,  # type: ignore[arg-type]
        on_exchange=on_exchange,
    )
    return client, script, ft, rng


def ok(body: bytes = b"[]") -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


REQ = UpstreamRequest(method="GET", url=URL, params={"a": "1"})


def test_success_returns_payload_with_provenance() -> None:
    client, _, _, _ = build([ok(b'{"x":1}')], takes=0.25)

    raw = client.send(REQ)

    assert raw.status_code == 200
    assert raw.body == b'{"x":1}'
    assert raw.content_type == "application/json"
    assert raw.sha256 == hashlib.sha256(b'{"x":1}').hexdigest()
    assert raw.duration_ms == 250
    assert raw.fetched_at.tzinfo is not None
    assert raw.fetched_at.utcoffset() == dt.timedelta(0)
    assert raw.request == REQ


def test_sends_honest_user_agent_and_params_without_following_redirects() -> None:
    client, script, _, _ = build([ok()])

    client.send(REQ)

    sent = script.calls[0]
    assert sent.headers["user-agent"] == DEFAULT_USER_AGENT
    assert sent.url.params["a"] == "1"
    assert sent.method == "GET"


def test_custom_user_agent_comes_from_settings() -> None:
    client, script, _, _ = build([ok()], user_agent="my-agent/9 (+mailto:me@example.test)")

    client.send(REQ)

    assert script.calls[0].headers["user-agent"] == "my-agent/9 (+mailto:me@example.test)"


def test_post_sends_form_data_and_extra_headers() -> None:
    client, script, _, _ = build([ok()])
    req = UpstreamRequest(method="POST", url=URL, form={"k": "v"}, headers={"X-Test": "1"})

    client.send(req)

    assert script.calls[0].method == "POST"
    assert script.calls[0].content == b"k=v"
    assert script.calls[0].headers["x-test"] == "1"


def test_kill_switch_raises_without_network() -> None:
    client, script, _, _ = build([ok()], upstream_enabled=False)

    with pytest.raises(UpstreamError, match="disabled") as exc:
        client.send(REQ)

    assert script.calls == []
    assert exc.value.url == URL


def test_waits_min_interval_between_requests_to_same_host() -> None:
    client, script, ft, _ = build([ok()], takes=0.5)

    client.send(REQ)
    client.send(REQ)

    assert script.call_times[1] - script.call_times[0] == pytest.approx(2.0)
    assert ft.sleeps == [pytest.approx(1.5)]


def test_no_wait_when_interval_already_elapsed_or_different_host() -> None:
    client, _, ft, _ = build([ok()])

    client.send(REQ)
    client.send(UpstreamRequest(method="GET", url="https://other.test/x"))
    ft.now += 10
    client.send(REQ)

    assert ft.sleeps == []


def test_retries_503_then_succeeds_with_full_jitter_backoff() -> None:
    client, script, ft, rng = build([httpx.Response(503), httpx.Response(503), ok()])

    raw = client.send(REQ)

    assert raw.status_code == 200
    assert len(script.calls) == 3
    assert rng.bounds == [1.0, 2.0]  # base * 2**(attempt-1)
    # each gap is jitter (here the max) and at least the 2 s politeness interval
    assert script.call_times[1] - script.call_times[0] >= 2.0
    assert ft.sleeps[0] == pytest.approx(1.0)


def test_backoff_is_capped_by_backoff_max() -> None:
    client, _, _, rng = build(
        [httpx.Response(500)], max_attempts=4, backoff_base_seconds=10.0, backoff_max_seconds=15.0
    )

    with pytest.raises(UpstreamError):
        client.send(REQ)

    assert rng.bounds == [10.0, 15.0, 15.0]


def test_gives_up_after_max_attempts_and_reports_status() -> None:
    client, script, _, _ = build([httpx.Response(502)], max_attempts=3)

    with pytest.raises(UpstreamError, match="3 attempts") as exc:
        client.send(REQ)

    assert len(script.calls) == 3
    assert exc.value.status_code == 502
    assert exc.value.url == URL


def test_429_is_retried() -> None:
    client, script, _, _ = build([httpx.Response(429), ok()])

    assert client.send(REQ).status_code == 200
    assert len(script.calls) == 2


def test_retry_after_seconds_replaces_backoff() -> None:
    client, script, _, rng = build([httpx.Response(429, headers={"Retry-After": "7"}), ok()])

    client.send(REQ)

    assert rng.bounds == []  # jitter not used
    assert script.call_times[1] - script.call_times[0] == pytest.approx(7.0)


def test_retry_after_is_capped_at_backoff_max() -> None:
    client, script, _, _ = build(
        [httpx.Response(503, headers={"Retry-After": "3600"}), ok()], backoff_max_seconds=20.0
    )

    client.send(REQ)

    assert script.call_times[1] - script.call_times[0] == pytest.approx(20.0)


def test_retry_after_http_date_in_past_means_no_extra_wait() -> None:
    past = "Wed, 21 Oct 2015 07:28:00 GMT"
    client, script, _, _ = build([httpx.Response(503, headers={"Retry-After": past}), ok()])

    client.send(REQ)

    # only the 2 s politeness spacing remains
    assert script.call_times[1] - script.call_times[0] == pytest.approx(2.0)


def test_parse_retry_after_variants() -> None:
    now = dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=dt.UTC)

    assert parse_retry_after("5", now) == 5.0
    assert parse_retry_after(" 2.5 ", now) == 2.5
    assert parse_retry_after("-3", now) == 0.0
    assert parse_retry_after("Fri, 02 Oct 2026 12:00:30 GMT", now) == 30.0
    assert parse_retry_after("Fri, 02 Oct 2026 11:00:00 GMT", now) == 0.0
    assert parse_retry_after("Fri, 02 Oct 2026 12:00:30", now) == 30.0  # naive date = UTC
    assert parse_retry_after(None, now) is None
    assert parse_retry_after("", now) is None
    assert parse_retry_after("soon", now) is None
    assert parse_retry_after("nan", now) is None
    assert parse_retry_after("inf", now) is None


def test_404_is_not_retried() -> None:
    client, script, _, _ = build([httpx.Response(404)])

    with pytest.raises(UpstreamError) as exc:
        client.send(REQ)

    assert len(script.calls) == 1
    assert exc.value.status_code == 404
    assert exc.value.url == URL


def test_redirect_is_an_error_and_not_followed() -> None:
    client, script, _, _ = build(
        [httpx.Response(302, headers={"Location": "https://elsewhere.test/"})]
    )

    with pytest.raises(UpstreamError, match="redirect") as exc:
        client.send(REQ)

    assert len(script.calls) == 1
    assert exc.value.status_code == 302


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")],
    ids=["timeout", "transport"],
)
def test_timeouts_and_transport_errors_are_retried(error: Exception) -> None:
    client, script, _, _ = build([error, ok()])

    assert client.send(REQ).status_code == 200
    assert len(script.calls) == 2


def test_persistent_timeout_raises_upstream_error() -> None:
    client, script, _, _ = build([httpx.ReadTimeout("slow")], max_attempts=2)

    with pytest.raises(UpstreamError, match="ReadTimeout") as exc:
        client.send(REQ)

    assert len(script.calls) == 2
    assert exc.value.status_code is None


def test_budget_is_spent_per_attempt_and_then_exhausted() -> None:
    client, script, _, _ = build([httpx.Response(503), ok()], request_budget=2, max_attempts=4)

    client.send(REQ)  # uses 2 attempts
    with pytest.raises(UpstreamError, match="request budget exhausted"):
        client.send(REQ)

    assert len(script.calls) == 2


def test_budget_exhausted_mid_retry_stops_immediately() -> None:
    client, script, _, _ = build([httpx.Response(503)], request_budget=2, max_attempts=4)

    with pytest.raises(UpstreamError, match="request budget exhausted"):
        client.send(REQ)

    assert len(script.calls) == 2


def _trip(client: PoliteClient, times: int) -> None:
    for _ in range(times):
        with pytest.raises(UpstreamError, match="giving up"):
            client.send(REQ)


def test_circuit_opens_then_half_opens_then_closes() -> None:
    client, script, ft, _ = build(
        [httpx.Response(500), httpx.Response(500), httpx.Response(500), ok()],
        max_attempts=1,
        breaker_failure_threshold=2,
        breaker_cooldown_seconds=100.0,
    )
    _trip(client, 2)
    assert len(script.calls) == 2

    with pytest.raises(UpstreamError, match="circuit open"):
        client.send(REQ)
    assert len(script.calls) == 2  # no network while open

    ft.now += 100.0  # cooldown over: one trial allowed, it fails -> re-open
    _trip(client, 1)
    assert len(script.calls) == 3
    with pytest.raises(UpstreamError, match="circuit open"):
        client.send(REQ)
    assert len(script.calls) == 3

    ft.now += 100.0  # trial succeeds -> closed
    assert client.send(REQ).status_code == 200
    assert client.send(REQ).status_code == 200
    assert len(script.calls) == 5


def test_circuit_is_per_host_and_success_resets_failure_count() -> None:
    client, script, _, _ = build(
        [httpx.Response(500), ok(), httpx.Response(500), ok()],
        max_attempts=1,
        breaker_failure_threshold=2,
    )
    other = UpstreamRequest(method="GET", url="https://other.test/x")

    _trip(client, 1)
    client.send(REQ)  # success resets the consecutive-failure count
    _trip(client, 1)  # only 1 consecutive failure again: still closed
    assert client.send(REQ).status_code == 200
    assert client.send(other).status_code == 200
    assert len(script.calls) == 5


def test_client_errors_do_not_trip_the_circuit() -> None:
    client, script, _, _ = build([httpx.Response(404)], breaker_failure_threshold=2, max_attempts=1)

    for _ in range(4):
        with pytest.raises(UpstreamError, match="HTTP 404"):
            client.send(REQ)

    assert len(script.calls) == 4


def test_exchange_event_emitted_for_every_attempt() -> None:
    events: list[ExchangeEvent] = []
    client, _, _, _ = build(
        [httpx.Response(503, content=b"busy"), httpx.ReadTimeout("slow"), ok(b"[1]")],
        events=events,
        takes=0.1,
    )

    client.send(REQ)

    assert [e.attempt for e in events] == [1, 2, 3]
    first, second, third = events
    assert first.status_code == 503
    assert first.error == "HTTP 503"
    assert first.bytes == 4
    assert second.status_code is None
    assert second.error is not None
    assert "ReadTimeout" in second.error
    assert second.sha256 is None
    assert third.status_code == 200
    assert third.error is None
    assert third.bytes == 3
    assert third.sha256 == hashlib.sha256(b"[1]").hexdigest()
    assert third.duration_ms == 100
    assert third.fetched_at.tzinfo is not None
    assert all(e.request == REQ for e in events)


def test_exchange_event_is_frozen() -> None:
    events: list[ExchangeEvent] = []
    client, _, _, _ = build([ok()], events=events)
    client.send(REQ)

    with pytest.raises(AttributeError):
        events[0].attempt = 9  # type: ignore[misc]


def test_context_manager_closes_client() -> None:
    ft = FakeTime()
    script = Script([ok()], ft)
    with PoliteClient(
        make_settings(), transport=httpx.MockTransport(script), clock=ft.clock, sleep=ft.sleep
    ) as client:
        assert client.send(REQ).status_code == 200
    with pytest.raises(RuntimeError):
        client.send(REQ)


def test_defaults_construct_without_injection() -> None:
    client = PoliteClient(make_settings(upstream_enabled=False))
    with pytest.raises(UpstreamError):
        client.send(REQ)
    client.close()
