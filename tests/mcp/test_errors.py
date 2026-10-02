"""Layer-1 contract evals: every error path is an isError result with {code, message, hint}."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import imda.mcp.tools.calendar as calendar_tools
import imda.mcp.tools.fx as fx_tools
from imda.config import Settings
from imda.mcp.errors import ERROR_CODES
from imda.mcp.server import build_server
from mcp import Client
from tests.mcp.conftest import FRESH_NOW
from tests.mcp.helpers import call, error_of, text_of

pytestmark = pytest.mark.anyio

MUMBAI_OFFICE = {"office": "mumbai"}


async def fail(client: Client, name: str, **arguments: Any) -> dict[str, Any]:
    return error_of(await client.call_tool(name, arguments))


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [
        # INVALID_REQUEST: bad type, format or range
        ("check_business_day", {"date": "2026-13-01", "office": "mumbai"}, "INVALID_REQUEST"),
        ("check_business_day", {"date": "27/03/2026", "office": "mumbai"}, "INVALID_REQUEST"),
        ("check_business_day", {"date": "1999-12-31", "office": "mumbai"}, "INVALID_REQUEST"),
        ("check_business_day", {"date": "2101-01-01", "office": "mumbai"}, "INVALID_REQUEST"),
        ("check_business_day", {"date": 20260327, "office": "mumbai"}, "INVALID_REQUEST"),
        ("check_business_day", {"office": "mumbai"}, "INVALID_REQUEST"),
        (
            "fetch_next_business_days",
            {"date": "2026-03-27", "office": "mumbai", "count": 0},
            "INVALID_REQUEST",
        ),
        (
            "fetch_next_business_days",
            {"date": "2026-03-27", "office": "mumbai", "count": 32},
            "INVALID_REQUEST",
        ),
        (
            "fetch_next_business_days",
            {"date": "2026-03-27", "office": "mumbai", "count": "x"},
            "INVALID_REQUEST",
        ),
        ("fetch_holidays", {"office": "mumbai", "year": 2026, "month": 13}, "INVALID_REQUEST"),
        ("fetch_holidays", {"office": "mumbai", "year": 1900}, "INVALID_REQUEST"),
        ("fetch_fx_rate", {"currency": "XYZ", "date": "2026-09-14"}, "INVALID_REQUEST"),
        ("fetch_fx_rate", {"currency": "INR", "date": "2026-09-14"}, "INVALID_REQUEST"),
        (
            "fetch_fx_rate",
            {"currency": "USD", "date": "2026-09-14", "source": "ecb"},
            "INVALID_REQUEST",
        ),
        (
            "fetch_all_fx_rates",
            {"currency": "USD", "from_date": "2026-09-01", "to_date": "2026-09-30", "limit": 1001},
            "INVALID_REQUEST",
        ),
        (
            "fetch_all_fx_rates",
            {
                "currency": "USD",
                "from_date": "2026-09-01",
                "to_date": "2026-09-30",
                "cursor": "not-a-cursor!",
            },
            "INVALID_REQUEST",
        ),
        (
            "convert_currency",
            {"amount": "abc", "from_currency": "USD", "to_currency": "INR", "date": "2026-09-14"},
            "INVALID_REQUEST",
        ),
        (
            "convert_currency",
            {"amount": "1", "from_currency": "CHF", "to_currency": "INR", "date": "2026-09-14"},
            "INVALID_REQUEST",
        ),
        (
            "estimate_settlement_date",
            {"captured_at": "2026-03-27T11:00:00", "office": "mumbai"},
            "INVALID_REQUEST",
        ),
        (
            "estimate_settlement_date",
            {"captured_at": "yesterday", "office": "mumbai"},
            "INVALID_REQUEST",
        ),
        (
            "estimate_settlement_date",
            {"captured_at": "2026-03-27T11:00:00+05:30", "office": "mumbai", "cycle_days": 31},
            "INVALID_REQUEST",
        ),
        (
            "estimate_settlement_date",
            {"captured_at": "2026-03-27T11:00:00+05:30", "office": "mumbai", "mode": "fast"},
            "INVALID_REQUEST",
        ),
        (
            "quote_invoice",
            {"amount": "x", "currency": "USD", "invoice_date": "2026-09-14", "office": "mumbai"},
            "INVALID_REQUEST",
        ),
        (
            "fetch_fx_stats",
            {
                "currency": "USD",
                "from_date": "2026-09-01",
                "to_date": "2026-09-30",
                "period": "year",
            },
            "INVALID_REQUEST",
        ),
        ("no_such_tool", {}, "INVALID_REQUEST"),
        # VALIDATION_ERROR: well-formed but rejected by the domain
        (
            "convert_currency",
            {"amount": "0", "from_currency": "USD", "to_currency": "INR", "date": "2026-09-14"},
            "VALIDATION_ERROR",
        ),
        (
            "convert_currency",
            {"amount": "-5", "from_currency": "USD", "to_currency": "INR", "date": "2026-09-14"},
            "VALIDATION_ERROR",
        ),
        (
            "convert_currency",
            {"amount": "1.234", "from_currency": "USD", "to_currency": "INR", "date": "2026-09-14"},
            "VALIDATION_ERROR",
        ),
        (
            "convert_currency",
            {"amount": "1", "from_currency": "USD", "to_currency": "usd", "date": "2026-09-14"},
            "VALIDATION_ERROR",
        ),
        ("fetch_mibor", {"from_date": "2026-09-30", "to_date": "2026-09-01"}, "VALIDATION_ERROR"),
        (
            "fetch_fx_stats",
            {"currency": "USD", "from_date": "2026-09-30", "to_date": "2026-09-01"},
            "VALIDATION_ERROR",
        ),
        (
            "quote_invoice",
            {"amount": "0.00", "currency": "USD", "invoice_date": "2026-09-14", "office": "mumbai"},
            "VALIDATION_ERROR",
        ),
        # OFFICE_NOT_FOUND
        ("fetch_holidays", {"office": "atlantis", "year": 2026}, "OFFICE_NOT_FOUND"),
        ("check_business_day", {"date": "2026-03-27", "office": "gotham"}, "OFFICE_NOT_FOUND"),
        ("fetch_next_business_days", {"date": "2026-03-27", "office": ""}, "OFFICE_NOT_FOUND"),
        (
            "estimate_settlement_date",
            {"captured_at": "2026-03-27T11:00:00+05:30", "office": "pune"},
            "OFFICE_NOT_FOUND",
        ),
        (
            "quote_invoice",
            {"amount": "1", "currency": "USD", "invoice_date": "2026-09-14", "office": "pune"},
            "OFFICE_NOT_FOUND",
        ),
        # RATE_NOT_FOUND
        ("fetch_fx_rate", {"currency": "USD", "date": "2026-12-01"}, "RATE_NOT_FOUND"),
        ("fetch_fx_rate", {"currency": "USD", "date": "2010-01-04"}, "RATE_NOT_FOUND"),
        (
            "convert_currency",
            {"amount": "1", "from_currency": "USD", "to_currency": "INR", "date": "2030-01-01"},
            "RATE_NOT_FOUND",
        ),
        (
            "quote_invoice",
            {"amount": "1", "currency": "USD", "invoice_date": "2030-01-01", "office": "mumbai"},
            "RATE_NOT_FOUND",
        ),
        # CALENDAR_DATA_MISSING
        ("fetch_holidays", {"office": "mumbai", "year": 2027}, "CALENDAR_DATA_MISSING"),
        ("fetch_holidays", {"office": "chennai", "year": 2026}, "CALENDAR_DATA_MISSING"),
        ("check_business_day", {"date": "2027-01-04", "office": "mumbai"}, "CALENDAR_DATA_MISSING"),
        (
            "fetch_next_business_days",
            {"date": "2026-12-30", "office": "mumbai"},
            "CALENDAR_DATA_MISSING",
        ),
        (
            "estimate_settlement_date",
            {"captured_at": "2026-12-30T11:00:00+05:30", "office": "mumbai"},
            "CALENDAR_DATA_MISSING",
        ),
        # RANGE_TOO_LARGE
        ("fetch_mibor", {"from_date": "2025-01-01", "to_date": "2026-09-30"}, "RANGE_TOO_LARGE"),
        (
            "fetch_all_fx_rates",
            {"currency": "USD", "from_date": "2000-01-01", "to_date": "2026-09-30"},
            "RANGE_TOO_LARGE",
        ),
        (
            "fetch_fx_stats",
            {"currency": "USD", "from_date": "2000-01-01", "to_date": "2026-09-30"},
            "RANGE_TOO_LARGE",
        ),
        (
            "compare_fx_sources",
            {"currency": "USD", "from_date": "2000-01-01", "to_date": "2026-09-30"},
            "RANGE_TOO_LARGE",
        ),
    ],
)
async def test_error_codes(client: Client, tool: str, arguments: dict[str, Any], code: str) -> None:
    body = await fail(client, tool, **arguments)

    assert body["code"] == code
    assert body["code"] in ERROR_CODES


async def test_office_not_found_suggests_a_close_slug_and_the_listing_tool(client: Client) -> None:
    body = await fail(client, "check_business_day", date="2026-03-27", office="mumbay")

    assert body["code"] == "OFFICE_NOT_FOUND"
    assert "fetch_all_offices" in body["hint"]
    assert "mumbai" in body["hint"]


async def test_rate_not_found_hint_points_to_recovery(client: Client) -> None:
    body = await fail(client, "fetch_fx_rate", currency="USD", date="2026-12-01")

    assert "earlier date" in body["hint"]
    assert "fetch_all_fx_rates" in body["hint"]


async def test_calendar_missing_hint_tells_the_model_to_tell_the_user(client: Client) -> None:
    body = await fail(client, "fetch_holidays", office="mumbai", year=2027)

    assert "2027" in body["hint"]
    assert "tell the user" in body["hint"].lower()


async def test_invalid_request_names_the_field_and_does_not_echo_secrets(client: Client) -> None:
    body = await fail(client, "check_business_day", date="not a date", office="mumbai")

    assert body["code"] == "INVALID_REQUEST"
    assert "date" in body["message"]
    assert "YYYY-MM-DD" in body["message"] + body["hint"]


async def test_stats_period_cap_is_range_too_large(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fx_tools, "MAX_STATS_PERIODS", 1)

    body = await fail(
        client,
        "fetch_fx_stats",
        currency="USD",
        from_date="2026-08-01",
        to_date="2026-09-30",
        period="week",
    )

    assert body["code"] == "RANGE_TOO_LARGE"
    assert "month" in body["hint"]


async def test_missing_database_is_store_unavailable(tmp_path: Path) -> None:
    from imda.config import Settings as Cfg

    server = build_server(
        Cfg(db_path=tmp_path / "missing.sqlite3", _env_file=None),  # type: ignore[call-arg]
        now=lambda: FRESH_NOW,
    )

    async with Client(server) as client:
        results = [
            await call(client, "fetch_all_offices"),
            await call(client, "fetch_fx_rate", currency="USD", date="2026-09-14"),
            await call(client, "fetch_source_health"),
        ]

    for result in results:
        body = error_of(result)
        assert body["code"] == "STORE_UNAVAILABLE"
        assert "missing.sqlite3" not in text_of(result)


async def test_unexpected_exception_is_a_generic_internal_error(
    client: Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(_: object) -> object:
        raise RuntimeError("secret-internal-detail /etc/passwd")

    monkeypatch.setattr(calendar_tools, "_offices", explode)

    with caplog.at_level("ERROR", logger="imda.mcp"):
        result = await call(client, "fetch_all_offices")

    body = error_of(result)
    assert body["code"] == "INTERNAL_ERROR"
    assert "secret-internal-detail" not in text_of(result)
    assert "secret-internal-detail" in caplog.text


async def test_errors_never_raise_to_the_host(client: Client) -> None:
    # Every call above returned a result; this one has arguments of the wrong shape entirely.
    result = await client.call_tool("fetch_holidays", {"office": ["a"], "year": {"x": 1}})

    assert error_of(result)["code"] == "INVALID_REQUEST"


def test_error_code_catalogue_matches_the_plan() -> None:
    assert set(ERROR_CODES) == {
        "VALIDATION_ERROR",
        "INVALID_REQUEST",
        "OFFICE_NOT_FOUND",
        "RATE_NOT_FOUND",
        "CALENDAR_DATA_MISSING",
        "RANGE_TOO_LARGE",
        "STORE_UNAVAILABLE",
        "INTERNAL_ERROR",
    }


async def test_error_results_carry_no_structured_content(
    client: Client, settings: Settings
) -> None:
    result = await call(client, "fetch_holidays", office="mumbai", year=2027)

    assert result.is_error
    assert result.structured_content is None


@pytest.mark.parametrize(
    "captured_at",
    [
        "9999-12-31T23:59:59+00:00",
        "0001-01-01T00:00:00+05:30",
        "2101-01-01T00:00:00+05:30",
        "1999-12-31T23:59:59+05:30",
    ],
)
async def test_out_of_range_captured_at_is_invalid_request(
    client: Client, captured_at: str
) -> None:
    settlement = await fail(
        client, "estimate_settlement_date", captured_at=captured_at, **MUMBAI_OFFICE
    )
    quote = await fail(
        client,
        "quote_invoice",
        amount="10.00",
        currency="USD",
        invoice_date="2026-09-24",
        captured_at=captured_at,
        **MUMBAI_OFFICE,
    )

    assert settlement["code"] == quote["code"] == "INVALID_REQUEST"
    assert "captured_at" in settlement["message"]
