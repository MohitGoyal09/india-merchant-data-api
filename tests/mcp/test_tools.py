"""Layer-1 contract evals: every tool's happy path, against exact values from the fixture DB.

Literal values were computed from the REST API on the same DB. Each test also checks parity:
the MCP result equals what ``GET /v1/...`` returns for the same question.
"""

from __future__ import annotations

from typing import Any

import pytest

from imda.config import Settings
from mcp import Client
from tests.mcp.helpers import call, data_of, ok, rest_client, text_of

pytestmark = pytest.mark.anyio


def rest_data(settings: Settings, path: str, **params: Any) -> dict[str, Any]:
    response = rest_client(settings).get(path, params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def has_boilerplate(structured: dict[str, Any]) -> None:
    assert structured["provenance"], "every result carries provenance"
    for item in structured["provenance"]:
        assert set(item) == {"source", "dataset", "source_url", "fetched_at", "stale"}
    assert isinstance(structured["warnings"], list)


async def test_fetch_all_offices(client: Client, settings: Settings) -> None:
    result = await ok(client, "fetch_all_offices")

    rest = rest_data(settings, "/v1/offices")
    assert result["count"] == 34
    assert result["offices"] == rest["data"]
    assert result["provenance"] == rest["provenance"]
    assert {"slug": "mumbai", "name": "Mumbai", "state": "Maharashtra", "rbi_id": 28} in result[
        "offices"
    ]
    has_boilerplate(result)


async def test_fetch_holidays_for_a_month(client: Client, settings: Settings) -> None:
    result = await ok(client, "fetch_holidays", office="mumbai", year=2026, month=3)

    rest = rest_data(settings, "/v1/holidays", office="mumbai", year=2026, month=3)
    assert result["count"] == 5
    assert [h["date"] for h in result["holidays"]] == [
        "2026-03-03",
        "2026-03-19",
        "2026-03-21",
        "2026-03-26",
        "2026-03-31",
    ]
    assert result["holidays"] == [
        {k: v for k, v in h.items() if k != "office"} for h in rest["data"]
    ]
    assert result["holidays"][3] == {
        "date": "2026-03-26",
        "weekday": "Thursday",
        "name": "Shree Ram Navami",
        "kind": "ni_act",
    }
    assert result["month"] == 3
    has_boilerplate(result)


async def test_fetch_holidays_whole_year_and_office_normalising(client: Client) -> None:
    year = await ok(client, "fetch_holidays", office="Mumbai", year=2026)
    delhi = await ok(client, "fetch_holidays", office=" New Delhi ", year=2001)

    assert year["month"] is None
    assert year["count"] == 40 - delhi["count"]
    assert delhi["office"] == "new-delhi"


async def test_check_business_day_for_a_weekend_and_a_working_day(
    client: Client, settings: Settings
) -> None:
    saturday = await ok(client, "check_business_day", date="2026-03-28", office="mumbai")
    monday = await ok(client, "check_business_day", date="2026-03-30", office="mumbai")

    rest = rest_data(settings, "/v1/calendar/business-day", date="2026-03-28", office="mumbai")
    assert data_of(saturday) == rest["data"]
    assert data_of(saturday) == {
        "date": "2026-03-28",
        "office": "mumbai",
        "weekday": "Saturday",
        "is_business_day": False,
        "reason": "4th Saturday",
    }
    assert monday["is_business_day"] is True
    assert monday["reason"] is None
    has_boilerplate(saturday)


async def test_check_business_day_names_a_holiday(client: Client) -> None:
    result = await ok(client, "check_business_day", date="2026-03-26", office="mumbai")

    assert result["is_business_day"] is False
    assert result["reason"] == "Shree Ram Navami"


async def test_fetch_next_business_days(client: Client, settings: Settings) -> None:
    result = await ok(
        client, "fetch_next_business_days", date="2026-03-27", office="mumbai", count=3
    )

    rest = rest_data(
        settings, "/v1/calendar/next-business-days", date="2026-03-27", office="mumbai", n=3
    )
    assert result["dates"] == rest["data"]["dates"] == ["2026-03-30", "2026-04-02", "2026-04-04"]
    assert result["count"] == 3
    assert result["after"] == "2026-03-27"
    has_boilerplate(result)


async def test_fetch_next_business_days_defaults_to_five(client: Client) -> None:
    result = await ok(client, "fetch_next_business_days", date="2026-03-27", office="mumbai")

    assert result["count"] == 5
    assert len(result["dates"]) == 5


async def test_estimate_settlement_date(client: Client, settings: Settings) -> None:
    result = await ok(
        client,
        "estimate_settlement_date",
        captured_at="2026-03-27T11:00:00+05:30",
        office="mumbai",
    )

    rest = rest_data(
        settings, "/v1/settlement/eta", captured_at="2026-03-27T11:00:00+05:30", office="mumbai"
    )
    assert data_of(result) == rest["data"]
    assert result["eta_date"] == "2026-04-02"
    assert result["cycle_days"] == 2
    assert result["mode"] == "working_days"
    assert [s["date"] for s in result["skipped"]] == [
        "2026-03-28",
        "2026-03-29",
        "2026-03-31",
        "2026-04-01",
    ]
    assert [s["reason"] for s in result["skipped"]][:2] == ["4th Saturday", "Sunday"]
    assert "estimate" in result["disclaimer"].lower()
    has_boilerplate(result)


async def test_estimate_settlement_modes_and_cycle(client: Client, settings: Settings) -> None:
    result = await ok(
        client,
        "estimate_settlement_date",
        captured_at="2026-03-27T11:00:00+05:30",
        office="mumbai",
        cycle_days=3,
        mode="calendar_then_roll",
    )

    rest = rest_data(
        settings,
        "/v1/settlement/eta",
        captured_at="2026-03-27T11:00:00+05:30",
        office="mumbai",
        cycle_days=3,
        mode="calendar_then_roll",
    )
    assert data_of(result) == rest["data"]
    assert result["mode"] == "calendar_then_roll"
    assert result["counted_days"] == []


async def test_estimate_uses_ist_date_of_the_capture(client: Client) -> None:
    # 20:00 UTC on 03-26 is 01:30 IST on 03-27.
    result = await ok(
        client,
        "estimate_settlement_date",
        captured_at="2026-03-26T20:00:00+00:00",
        office="mumbai",
    )

    assert result["capture_date"] == "2026-03-27"


async def test_quote_invoice_with_settlement(client: Client, settings: Settings) -> None:
    arguments = {
        "amount": "1200.00",
        "currency": "USD",
        "invoice_date": "2026-09-14",
        "office": "mumbai",
        "captured_at": "2026-09-14T11:00:00+05:30",
    }

    result = await ok(client, "quote_invoice", **arguments)

    rest = rest_client(settings).post("/v1/invoice/quote", json=arguments).json()
    conversion = rest["data"]["conversion"]
    conversion = {
        ("from_currency" if k == "from" else "to_currency" if k == "to" else k): v
        for k, v in conversion.items()
    }
    assert result["conversion"] == conversion
    assert result["settlement"] == rest["data"]["settlement"]
    assert result["notes"] == rest["data"]["notes"]
    assert result["conversion"]["result"] == "114869.40"
    assert result["conversion"]["rates_used"][0]["effective_date"] == "2026-09-11"
    assert result["settlement"]["eta_date"] == "2026-09-16"
    assert result["provenance"] == rest["provenance"]
    has_boilerplate(result)


async def test_quote_invoice_without_capture_has_no_settlement(client: Client) -> None:
    result = await ok(
        client,
        "quote_invoice",
        amount="10",
        currency="eur",
        invoice_date="2026-09-10",
        office="mumbai",
    )

    assert result["settlement"] is None
    assert result["conversion"]["from_currency"] == "EUR"
    assert [p["dataset"] for p in result["provenance"]] == ["fx_reference_rates"]


async def test_fetch_fx_rate_falls_back_with_the_holiday_reason(
    client: Client, settings: Settings
) -> None:
    result = await ok(client, "fetch_fx_rate", currency="USD", date="2026-09-14")

    rest = rest_data(settings, "/v1/fx/rates/as-of", currency="USD", date="2026-09-14")
    assert data_of(result) == rest["data"]
    assert result["effective_date"] == "2026-09-11"
    assert result["lag_days"] == 3
    assert "Ganesh Chaturthi" in result["reason"]
    assert result["rate"]["rate"] == "95.7245"
    assert result["rate"]["source"] == "fbil"
    assert result["provenance"] == rest["provenance"]
    assert "Ganesh Chaturthi" in text_of(
        await call(client, "fetch_fx_rate", currency="USD", date="2026-09-14")
    )
    has_boilerplate(result)


async def test_fetch_fx_rate_on_a_publication_day_and_jpy_unit(client: Client) -> None:
    usd = await ok(client, "fetch_fx_rate", currency="usd", date="2026-09-11")
    jpy = await ok(client, "fetch_fx_rate", currency="JPY", date="2026-09-11")

    assert usd["lag_days"] == 0
    assert usd["reason"] is None
    assert (jpy["rate"]["unit"], jpy["rate"]["rate"], jpy["rate"]["rate_per_unit"]) == (
        100,
        "62.14",
        "0.6214",
    )


async def test_fetch_fx_rate_source_choice(client: Client, settings: Settings) -> None:
    result = await ok(client, "fetch_fx_rate", currency="USD", date="2018-07-10", source="rbi")

    rest = rest_data(
        settings, "/v1/fx/rates/as-of", currency="USD", date="2018-07-10", source="rbi"
    )
    assert data_of(result) == rest["data"]
    assert result["rate"]["source"] == "rbi"


async def test_fetch_all_fx_rates_pages_with_a_cursor(client: Client, settings: Settings) -> None:
    base = {"currency": "USD", "from_date": "2026-09-01", "to_date": "2026-09-30"}
    rest = rest_data(
        settings, "/v1/fx/rates", currency="USD", **{"from": "2026-09-01"}, to="2026-09-30"
    )

    first = await ok(client, "fetch_all_fx_rates", **base, limit=10)
    second = await ok(client, "fetch_all_fx_rates", **base, limit=10, cursor=first["next_cursor"])
    third = await ok(client, "fetch_all_fx_rates", **base, limit=10, cursor=second["next_cursor"])

    assert first["count"] == 10
    assert first["next_cursor"] is not None
    assert third["next_cursor"] is None
    combined = first["rates"] + second["rates"] + third["rates"]
    assert combined == rest["data"]
    assert len(combined) == 21
    assert [r["date"] for r in combined] == sorted({r["date"] for r in combined})
    has_boilerplate(first)


async def test_fetch_all_fx_rates_default_limit_returns_the_whole_month(client: Client) -> None:
    result = await ok(
        client, "fetch_all_fx_rates", currency="GBP", from_date="2026-09-01", to_date="2026-09-30"
    )

    assert result["count"] == 21
    assert result["next_cursor"] is None
    assert result["from_date"] == "2026-09-01"


async def test_convert_currency(client: Client, settings: Settings) -> None:
    result = await ok(
        client,
        "convert_currency",
        amount="100",
        from_currency="USD",
        to_currency="inr",
        date="2026-09-14",
    )

    rest = rest_data(
        settings, "/v1/fx/convert", amount="100", date="2026-09-14", to="INR", **{"from": "USD"}
    )
    expected = {
        ("from_currency" if k == "from" else "to_currency" if k == "to" else k): v
        for k, v in rest["data"].items()
    }
    assert data_of(result) == expected
    assert result["result"] == "9572.45"
    assert result["is_cross_rate"] is False
    has_boilerplate(result)


async def test_convert_currency_cross_rate(client: Client) -> None:
    result = await ok(
        client,
        "convert_currency",
        amount="100",
        from_currency="USD",
        to_currency="JPY",
        date="2026-09-14",
    )

    assert result["is_cross_rate"] is True
    assert result["result"] == "15404.65"
    assert [r["currency"] for r in result["rates_used"]] == ["USD", "JPY"]


async def test_fetch_fx_stats(client: Client, settings: Settings) -> None:
    result = await ok(
        client, "fetch_fx_stats", currency="USD", from_date="2026-09-01", to_date="2026-09-30"
    )

    rest = rest_data(
        settings, "/v1/fx/stats", currency="USD", to="2026-09-30", **{"from": "2026-09-01"}
    )
    assert result["stats"] == rest["data"]
    assert result["count"] == 1
    assert result["stats"][0]["mean"] == "95.4641238095"
    assert result["stats"][0]["count"] == 21
    has_boilerplate(result)


async def test_fetch_fx_stats_weekly(client: Client, settings: Settings) -> None:
    result = await ok(
        client,
        "fetch_fx_stats",
        currency="USD",
        from_date="2026-09-01",
        to_date="2026-09-30",
        period="week",
    )

    rest = rest_data(
        settings,
        "/v1/fx/stats",
        currency="USD",
        period="week",
        to="2026-09-30",
        **{"from": "2026-09-01"},
    )
    assert result["stats"] == rest["data"]
    assert result["count"] >= 4


async def test_compare_fx_sources(client: Client, settings: Settings) -> None:
    result = await ok(
        client, "compare_fx_sources", currency="USD", from_date="2018-07-10", to_date="2018-07-24"
    )

    rest = rest_data(
        settings, "/v1/fx/compare", currency="USD", to="2018-07-24", **{"from": "2018-07-10"}
    )
    assert result["rows"] == rest["data"]["rows"]
    assert result["summary"] == rest["data"]["summary"]
    assert result["rows_scope"] == "all"
    assert result["summary"]["overlap_days"] == 1
    assert {p["source"] for p in result["provenance"]} == {"rbi", "fbil"}


async def test_fetch_mibor(client: Client, settings: Settings) -> None:
    result = await ok(client, "fetch_mibor", from_date="2026-09-01", to_date="2026-09-30")

    rest = rest_data(settings, "/v1/rates/mibor", to="2026-09-30", **{"from": "2026-09-01"})
    assert result["rates"] == rest["data"]
    assert result["count"] == 17
    friday = next(r for r in result["rates"] if r["date"] == "2026-09-04")
    assert (friday["tenor"], friday["spans_weekend"], friday["rate"]) == ("3D", True, "4.91")
    assert result["provenance"] == rest["provenance"]
    has_boilerplate(result)


async def test_fetch_source_health(client: Client, settings: Settings) -> None:
    result = await ok(client, "fetch_source_health")

    rest = rest_data(settings, "/v1/sources/health")
    assert data_of(result) == rest["data"]
    assert result["status"] == "ok"
    assert len(result["sources"]) == 5
    series = [
        s for s in result["sources"] if s["dataset"] != "offices" and s["dataset"] != "holidays"
    ]
    assert len(series) == 3
    assert all(s["freshness"] is not None and s["freshness"]["stale"] is False for s in series)
    assert result["provenance"] == rest["provenance"]
    has_boilerplate(result)


async def test_text_content_has_a_summary_and_the_data(client: Client) -> None:
    text = text_of(
        await call(
            client,
            "estimate_settlement_date",
            captured_at="2026-03-27T11:00:00+05:30",
            office="mumbai",
        )
    )

    summary, _, body = text.partition("\n\n")
    assert "2026-04-02" in summary
    assert "estimate" in summary.lower()
    assert '"eta_date":"2026-04-02"' in body
