"""``captured_at`` must land between 2000-01-01 and 2100-12-31 in IST (REST side)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import assert_error, body

ETA = "/v1/settlement/eta"
QUOTE = "/v1/invoice/quote"
QUOTE_BASE: dict[str, Any] = {
    "amount": "10.00",
    "currency": "USD",
    "invoice_date": "2026-09-24",
    "office": "mumbai",
}
OUT_OF_RANGE = [
    "9999-12-31T23:59:59%2B00:00",
    "0001-01-01T00:00:00%2B05:30",
    "2101-01-01T00:00:00%2B05:30",
    "1999-12-31T23:59:59%2B05:30",
]


@pytest.mark.parametrize("moment", OUT_OF_RANGE)
def test_eta_rejects_out_of_range_captured_at(client: TestClient, moment: str) -> None:
    assert_error(client.get(f"{ETA}?captured_at={moment}&office=mumbai"), 422, "INVALID_REQUEST")


@pytest.mark.parametrize("moment", [m.replace("%2B", "+") for m in OUT_OF_RANGE])
def test_quote_rejects_out_of_range_captured_at(client: TestClient, moment: str) -> None:
    payload = {**QUOTE_BASE, "captured_at": moment}

    assert_error(client.post(QUOTE, json=payload), 422, "INVALID_REQUEST")


def test_bounds_are_checked_in_ist_not_in_the_sent_offset(client: TestClient) -> None:
    # 2100-12-31 20:00 -05:00 is 2101-01-01 06:30 IST: out of range.
    late = "2100-12-31T20:00:00-05:00".replace("+", "%2B")
    assert_error(client.get(f"{ETA}?captured_at={late}&office=mumbai"), 422, "INVALID_REQUEST")


def test_in_range_captured_at_is_still_accepted(client: TestClient) -> None:
    parsed = body(client.get(f"{ETA}?captured_at=2026-03-27T11:00:00%2B05:30&office=mumbai"))

    assert parsed["data"]["eta_date"] == "2026-04-02"
