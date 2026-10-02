"""``POST /v1/invoice/quote``."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import assert_envelope, assert_error, body

URL = "/v1/invoice/quote"
BASE: dict[str, Any] = {
    "amount": "1250.50",
    "currency": "USD",
    "invoice_date": "2026-09-24",
    "office": "mumbai",
    "source": "fbil",
}


def test_quote_without_captured_at_has_no_settlement(client: TestClient) -> None:
    parsed = body(client.post(URL, json=BASE))

    assert_envelope(parsed)
    data = parsed["data"]
    assert data["settlement"] is None
    conversion = data["conversion"]
    assert conversion["amount"] == "1250.50"
    assert conversion["from"] == "USD"
    assert conversion["to"] == "INR"
    assert conversion["result"] == "119935.33"
    assert conversion["exact"] == "119935.329950"
    assert isinstance(conversion["result"], str)
    assert conversion["rates_used"][0]["rate"]["rate"] == "95.9099"
    assert data["notes"] == []
    assert [p["dataset"] for p in parsed["provenance"]] == ["fx_reference_rates"]


def test_quote_with_captured_at_adds_settlement_and_note(client: TestClient) -> None:
    payload = {**BASE, "captured_at": "2026-03-27T11:00:00+05:30"}

    parsed = body(client.post(URL, json=payload))

    settlement = parsed["data"]["settlement"]
    assert settlement["eta_date"] == "2026-04-02"
    assert settlement["office"] == "mumbai"
    assert "not Razorpay's settlement engine" in settlement["disclaimer"]
    assert any("indicative" in note for note in parsed["data"]["notes"])
    assert {p["dataset"] for p in parsed["provenance"]} == {"fx_reference_rates", "holidays"}


def test_quote_mode_and_cycle_days(client: TestClient) -> None:
    payload = {
        **BASE,
        "captured_at": "2026-03-27T11:00:00+05:30",
        "cycle_days": 2,
        "mode": "calendar_then_roll",
    }

    assert body(client.post(URL, json=payload))["data"]["settlement"]["eta_date"] == "2026-03-30"


def test_quote_notes_the_rate_date_when_the_invoice_date_has_no_rate(client: TestClient) -> None:
    parsed = body(client.post(URL, json={**BASE, "invoice_date": "2026-09-13"}))

    assert parsed["data"]["conversion"]["rates_used"][0]["effective_date"] == "2026-09-11"
    assert parsed["data"]["notes"] == ["rate is from 2026-09-11 (Sunday)"]


def test_quote_jpy_respects_the_per_100_unit(client: TestClient) -> None:
    parsed = body(client.post(URL, json={**BASE, "currency": "jpy", "amount": "100"}))

    assert parsed["data"]["conversion"]["result"] == "60.62"


@pytest.mark.parametrize(
    ("patch", "status", "code"),
    [
        ({"amount": 100}, 422, "INVALID_REQUEST"),
        ({"amount": 100.5}, 422, "INVALID_REQUEST"),
        ({"amount": "abc"}, 422, "INVALID_REQUEST"),
        ({"amount": "-5"}, 422, "VALIDATION_ERROR"),
        ({"amount": "0"}, 422, "VALIDATION_ERROR"),
        ({"amount": "10.123"}, 422, "VALIDATION_ERROR"),
        ({"currency": "XXX"}, 422, "INVALID_REQUEST"),
        ({"currency": "INR"}, 422, "INVALID_REQUEST"),
        ({"invoice_date": "24/09/2026"}, 422, "INVALID_REQUEST"),
        ({"captured_at": "2026-03-27T11:00:00"}, 422, "INVALID_REQUEST"),
        ({"cycle_days": 99}, 422, "INVALID_REQUEST"),
        ({"mode": "nope"}, 422, "INVALID_REQUEST"),
        ({"source": "xe"}, 422, "INVALID_REQUEST"),
        ({"surprise": 1}, 422, "INVALID_REQUEST"),
        ({"office": "oz"}, 404, "OFFICE_NOT_FOUND"),
        ({"invoice_date": "2015-01-05"}, 404, "RATE_NOT_FOUND"),
    ],
)
def test_quote_errors(client: TestClient, patch: dict[str, Any], status: int, code: str) -> None:
    assert_error(client.post(URL, json={**BASE, **patch}), status, code)


@pytest.mark.parametrize("missing", ["amount", "currency", "invoice_date", "office"])
def test_quote_requires_core_fields(client: TestClient, missing: str) -> None:
    payload = {k: v for k, v in BASE.items() if k != missing}

    parsed = assert_error(client.post(URL, json=payload), 422, "INVALID_REQUEST")

    assert parsed["error"]["details"]["errors"][0]["field"] == missing


def test_quote_rejects_malformed_json(client: TestClient) -> None:
    response = client.post(URL, content=b"{not json", headers={"content-type": "application/json"})

    assert_error(response, 422, "INVALID_REQUEST")
