"""Properties of FX pagination: complete, ordered, duplicate-free, and garbage-proof cursors."""

from __future__ import annotations

import base64
import datetime as dt

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from imda.api.deps import MAX_API_DATE, MIN_API_DATE
from imda.api.routes.fx import decode_cursor, encode_cursor
from imda.errors import InvalidInput
from imda.models import Currency

RATES = "/v1/fx/rates"
# The seeded DB holds Jul 2018, all of 2021 (FBIL only) and Sep 2026.
SEED_MIN, SEED_MAX = dt.date(2018, 7, 1), dt.date(2026, 9, 30)
api_dates = st.dates(min_value=MIN_API_DATE, max_value=MAX_API_DATE)
SEED_WINDOWS = (
    (dt.date(2018, 7, 1), dt.date(2018, 8, 15)),
    (dt.date(2021, 1, 1), dt.date(2021, 12, 31)),
    (dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
    (SEED_MIN, SEED_MAX),
)


@st.composite
def seeded_ranges(draw: st.DrawFn) -> tuple[dt.date, dt.date]:
    """A (start, end) pair inside a window where the seeded DB actually holds rates."""
    low, high = draw(st.sampled_from(SEED_WINDOWS))
    days = st.dates(min_value=low, max_value=high)
    first, second = sorted((draw(days), draw(days)))
    return first, second


garbage = st.one_of(
    st.text(max_size=80),
    st.binary(max_size=40).map(lambda b: b.hex()),
    st.text(max_size=40).map(lambda t: base64.urlsafe_b64encode(t.encode()).decode()),
    st.text(alphabet="0123456789-", max_size=12).map(
        lambda t: base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")
    ),
)
# The client fixture is session scoped (read-only DB), which Hypothesis cannot prove.
fixture_ok = settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@given(api_dates)
def test_cursor_round_trips(day: dt.date) -> None:
    assert decode_cursor(encode_cursor(day)) == day


def fetch(api: TestClient, params: dict[str, str]) -> tuple[list[tuple[str, str]], str | None]:
    response = api.get(RATES, params=params)
    assert response.status_code == 200, response.text
    parsed = response.json()
    rows = [(row["date"], row["source"]) for row in parsed["data"]]
    return rows, parsed["meta"]["next_cursor"]


@fixture_ok
@given(
    seeded_ranges(),
    st.sampled_from(list(Currency)),
    st.sampled_from(["auto", "rbi", "fbil"]),
    st.integers(min_value=1, max_value=4) | st.integers(min_value=1, max_value=40),
)
def test_walking_pages_yields_every_row_exactly_once_in_order(
    api: TestClient,
    window: tuple[dt.date, dt.date],
    currency: Currency,
    source: str,
    limit: int,
) -> None:
    start, end = window
    base = {
        "currency": currency.value,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "source": source,
    }
    expected, closing = fetch(api, base)  # default page size 1000 holds the whole seeded range
    assert closing is None
    event(f"pages needed: {min(-(-len(expected) // limit), 5)}")

    walked: list[tuple[str, str]] = []
    cursor: str | None = None
    for _ in range(len(expected) + 2):
        params = {**base, "limit": str(limit), **({"cursor": cursor} if cursor else {})}
        page, cursor = fetch(api, params)
        assert len(page) <= limit
        walked.extend(page)
        if cursor is None:
            break
    else:
        raise AssertionError("pagination did not terminate")

    assert walked == expected
    dates = [d for d, _ in walked]
    assert dates == sorted(set(dates))  # one row per date, oldest first


@fixture_ok
@given(garbage)
def test_garbled_cursors_get_422_not_a_server_error(api: TestClient, cursor: str) -> None:
    response = api.get(
        RATES,
        params={"currency": "USD", "from": "2026-09-01", "to": "2026-09-30", "cursor": cursor},
    )

    try:
        decode_cursor(cursor)
    except InvalidInput:
        assert response.status_code == 422, response.text
    else:
        assert response.status_code == 200, response.text


@fixture_ok
@given(st.dates(min_value=dt.date(1, 1, 1), max_value=dt.date(9999, 12, 31)))
def test_well_formed_cursors_for_any_date_are_handled(api: TestClient, day: dt.date) -> None:
    cursor = base64.urlsafe_b64encode(day.isoformat().encode()).decode().rstrip("=")
    response = api.get(
        RATES,
        params={"currency": "USD", "from": "2026-09-01", "to": "2026-09-30", "cursor": cursor},
    )

    in_bounds = MIN_API_DATE <= day <= MAX_API_DATE
    assert response.status_code == (200 if in_bounds else 422), response.text
