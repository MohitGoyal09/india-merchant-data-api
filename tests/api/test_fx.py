"""FX history (JSON, pagination, CSV), as-of, convert, stats and compare."""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient

from imda.api.routes.fx import decode_cursor, encode_cursor
from imda.config import Settings
from tests.api.conftest import MakeClient
from tests.api.helpers import assert_envelope, assert_error, body

RATES = "/v1/fx/rates"
YEAR_2021 = "currency=USD&from=2021-01-01&to=2021-12-31"


def assert_no_floats(node: object) -> None:
    if isinstance(node, float):
        raise AssertionError("float found in response")
    if isinstance(node, dict):
        for value in node.values():
            assert_no_floats(value)
    if isinstance(node, list):
        for value in node:
            assert_no_floats(value)


# ---------------------------------------------------------------- /fx/rates
def test_rates_row_shape_and_decimal_strings(client: TestClient) -> None:
    parsed = body(client.get(f"{RATES}?currency=USD&from=2026-09-21&to=2026-09-24&source=fbil"))

    assert_envelope(parsed)
    assert parsed["meta"]["count"] == 4
    assert parsed["meta"]["next_cursor"] is None
    assert parsed["data"][0] == {
        "date": "2026-09-21",
        "currency": "USD",
        "rate": "95.7991",
        "unit": 1,
        "rate_per_unit": "95.7991",
        "source": "fbil",
        "published_at": "2026-09-21T13:00:00+05:30",
    }
    assert [r["date"] for r in parsed["data"]] == sorted(r["date"] for r in parsed["data"])
    assert_no_floats(parsed)
    (prov,) = parsed["provenance"]
    assert prov["source"] == "fbil"
    assert prov["source_url"] == "https://www.fbil.org.in/wasdm/refrates/fetchfiltered"
    assert prov["fetched_at"] == "2026-09-25T04:00:00+00:00"
    assert prov["stale"] is False


def test_rates_jpy_unit_and_per_unit_value(client: TestClient) -> None:
    parsed = body(client.get(f"{RATES}?currency=jpy&from=2026-09-24&to=2026-09-24&source=fbil"))

    row = parsed["data"][0]
    assert (row["rate"], row["unit"], row["rate_per_unit"]) == ("60.62", 100, "0.6062")


def test_rates_auto_merges_fbil_first_with_rbi_failover_and_both_in_provenance(
    client: TestClient,
) -> None:
    parsed = body(client.get(f"{RATES}?currency=USD&from=2026-09-22&to=2026-09-28"))

    by_date = {r["date"]: r["source"] for r in parsed["data"]}
    assert by_date["2026-09-22"] == "fbil"
    assert by_date["2026-09-25"] == "rbi"
    assert {p["source"] for p in parsed["provenance"]} == {"fbil", "rbi"}


def test_rates_source_filter(client: TestClient) -> None:
    parsed = body(client.get(f"{RATES}?currency=USD&from=2026-09-22&to=2026-09-28&source=rbi"))

    assert {r["source"] for r in parsed["data"]} == {"rbi"}


def test_rates_pagination_round_trip_has_no_gap_or_overlap(client: TestClient) -> None:
    full = body(client.get(f"{RATES}?{YEAR_2021}"))
    assert full["meta"]["count"] == 241
    assert full["meta"]["next_cursor"] is None

    pages: list[dict] = []
    cursor = None
    while True:
        url = f"{RATES}?{YEAR_2021}&limit=100" + (f"&cursor={cursor}" if cursor else "")
        page = body(client.get(url))
        pages.append(page)
        cursor = page["meta"]["next_cursor"]
        if cursor is None:
            break

    assert [p["meta"]["count"] for p in pages] == [100, 100, 41]
    joined = [row for page in pages for row in page["data"]]
    assert joined == full["data"]
    assert len({r["date"] for r in joined}) == 241


def test_rates_cursor_is_opaque_base64url_of_the_last_date(client: TestClient) -> None:
    page = body(client.get(f"{RATES}?{YEAR_2021}&limit=3"))

    cursor = page["meta"]["next_cursor"]
    assert decode_cursor(cursor) == dt.date.fromisoformat(page["data"][-1]["date"])
    assert cursor == encode_cursor(decode_cursor(cursor))
    assert "=" not in cursor


def test_rates_exact_page_boundary_has_no_next_cursor(client: TestClient) -> None:
    page = body(client.get(f"{RATES}?{YEAR_2021}&limit=241"))

    assert page["meta"]["count"] == 241
    assert page["meta"]["next_cursor"] is None


def test_rates_cursor_past_the_end_gives_an_empty_page(client: TestClient) -> None:
    cursor = encode_cursor(dt.date(2021, 12, 31))

    parsed = body(client.get(f"{RATES}?{YEAR_2021}&cursor={cursor}"))

    assert parsed["data"] == []
    assert parsed["meta"]["next_cursor"] is None


def test_rates_limit_above_the_configured_maximum_is_rejected(
    settings: Settings, make_client: MakeClient
) -> None:
    small = settings.model_copy(update={"max_page_size": 50})
    client = make_client(custom=small)

    assert_error(client.get(f"{RATES}?{YEAR_2021}&limit=51"), 422, "INVALID_REQUEST")
    assert body(client.get(f"{RATES}?{YEAR_2021}"))["meta"]["count"] == 50  # default is capped


@pytest.mark.parametrize(
    ("query", "status", "code"),
    [
        ("currency=USD&from=2026-09-24&to=2026-09-21", 422, "VALIDATION_ERROR"),
        ("currency=USD&from=2000-01-01&to=2026-09-21", 422, "RANGE_TOO_LARGE"),
        ("currency=USD&from=2021-01-01&to=2021-02-01&cursor=!!!", 422, "INVALID_REQUEST"),
        ("currency=USD&from=2021-01-01&to=2021-02-01&limit=0", 422, "INVALID_REQUEST"),
        ("currency=XYZ&from=2021-01-01&to=2021-02-01", 422, "INVALID_REQUEST"),
        ("currency=USD&from=2021-01-01&to=2021-02-01&source=ecb", 422, "INVALID_REQUEST"),
        ("currency=USD&from=2021-1-1&to=2021-02-01", 422, "INVALID_REQUEST"),
        ("currency=USD&from=20210101&to=2021-02-01", 422, "INVALID_REQUEST"),
        ("currency=USD&from=1609459200&to=2021-02-01", 422, "INVALID_REQUEST"),
        ("currency=USD&from=2021-01-01", 422, "INVALID_REQUEST"),
        ("from=2021-01-01&to=2021-02-01", 422, "INVALID_REQUEST"),
    ],
)
def test_rates_errors(client: TestClient, query: str, status: int, code: str) -> None:
    assert_error(client.get(f"{RATES}?{query}"), status, code)


def test_rates_invalid_date_error_names_the_field(client: TestClient) -> None:
    parsed = assert_error(
        client.get(f"{RATES}?currency=USD&from=nope&to=2021-02-01"), 422, "INVALID_REQUEST"
    )

    (err,) = parsed["error"]["details"]["errors"]
    assert (err["field"], err["in"]) == ("from", "query")
    assert "ISO date" in err["message"]
    assert "nope" not in str(parsed)


# ---------------------------------------------------------------- CSV
def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_csv_via_format_param(client: TestClient) -> None:
    response = client.get(f"{RATES}?{YEAR_2021}&format=csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = _csv_rows(response.text)
    assert rows[0] == [
        "date",
        "currency",
        "rate",
        "unit",
        "rate_per_unit",
        "source",
        "published_at",
    ]
    assert len(rows) == 1 + 241
    assert rows[1][1] == "USD"
    assert response.headers["X-IMDA-Sources"] == "fbil"
    assert response.headers["X-IMDA-Degraded"] == "false"
    assert "fx_USD_2021-01-01_2021-12-31.csv" in response.headers["content-disposition"]


def test_csv_via_accept_header_ignores_pagination(client: TestClient) -> None:
    response = client.get(f"{RATES}?{YEAR_2021}&limit=5", headers={"Accept": "text/csv"})

    assert len(_csv_rows(response.text)) == 1 + 241


def test_csv_empty_values_are_blank(client: TestClient) -> None:
    response = client.get(
        f"{RATES}?currency=USD&from=2026-09-25&to=2026-09-25&format=csv&source=rbi"
    )

    header, row = _csv_rows(response.text)
    assert row[header.index("published_at")] == ""
    assert row[header.index("source")] == "rbi"


def test_format_json_wins_over_the_csv_accept_header(client: TestClient) -> None:
    response = client.get(
        f"{RATES}?currency=USD&from=2026-09-21&to=2026-09-22&format=json",
        headers={"Accept": "text/csv"},
    )

    assert_envelope(body(response))


def test_csv_range_is_capped(client: TestClient) -> None:
    assert_error(
        client.get(f"{RATES}?currency=USD&from=2000-01-01&to=2026-01-01&format=csv"),
        422,
        "RANGE_TOO_LARGE",
    )


# ---------------------------------------------------------------- as-of
def test_as_of_exact_date(client: TestClient) -> None:
    parsed = body(client.get("/v1/fx/rates/as-of?currency=USD&date=2026-09-22&source=fbil"))

    assert parsed["data"]["effective_date"] == "2026-09-22"
    assert parsed["data"]["reason"] is None
    assert parsed["data"]["lag_days"] == 0
    assert parsed["data"]["rate"]["rate"] == "95.8179"


def test_as_of_holiday_explains_the_gap(client: TestClient) -> None:
    parsed = body(client.get("/v1/fx/rates/as-of?currency=USD&date=2026-09-14&source=fbil"))

    data = parsed["data"]
    assert data["requested_date"] == "2026-09-14"
    assert data["effective_date"] == "2026-09-11"
    assert data["lag_days"] == 3
    assert data["reason"].startswith("Ganesh Chaturthi")


def test_as_of_weekend(client: TestClient) -> None:
    parsed = body(client.get("/v1/fx/rates/as-of?currency=USD&date=2026-09-13&source=fbil"))

    assert (parsed["data"]["effective_date"], parsed["data"]["reason"]) == ("2026-09-11", "Sunday")


def test_as_of_future_date_is_not_yet_published(client: TestClient) -> None:
    parsed = body(client.get("/v1/fx/rates/as-of?currency=USD&date=2026-10-03"))

    assert parsed["data"]["reason"] == "not yet published"


def test_as_of_without_data_is_rate_not_found(client: TestClient) -> None:
    parsed = assert_error(
        client.get("/v1/fx/rates/as-of?currency=USD&date=2015-01-05"), 404, "RATE_NOT_FOUND"
    )

    assert parsed["error"]["details"] == {"currency": "USD", "date": "2015-01-05"}


def test_as_of_validation(client: TestClient) -> None:
    assert_error(client.get("/v1/fx/rates/as-of?currency=USD"), 422, "INVALID_REQUEST")
    assert_error(
        client.get("/v1/fx/rates/as-of?currency=USD&date=yesterday"), 422, "INVALID_REQUEST"
    )


# ---------------------------------------------------------------- convert
CONVERT = "/v1/fx/convert"


def test_convert_foreign_to_inr(client: TestClient) -> None:
    parsed = body(client.get(f"{CONVERT}?amount=100&from=USD&to=INR&date=2026-09-24&source=fbil"))

    data = parsed["data"]
    assert (data["result"], data["from"], data["to"]) == ("9590.99", "USD", "INR")
    assert data["is_cross_rate"] is False
    assert data["rates_used"][0]["rate"]["rate"] == "95.9099"
    assert_no_floats(parsed)


def test_convert_jpy_respects_unit(client: TestClient) -> None:
    parsed = body(client.get(f"{CONVERT}?amount=100&from=JPY&to=INR&date=2026-09-24&source=fbil"))

    assert parsed["data"]["result"] == "60.62"


def test_convert_inr_to_foreign_and_cross_rate(client: TestClient) -> None:
    back = body(client.get(f"{CONVERT}?amount=9590.99&from=INR&to=usd&date=2026-09-24&source=fbil"))
    cross = body(client.get(f"{CONVERT}?amount=100&from=USD&to=EUR&date=2026-09-24&source=fbil"))

    assert back["data"]["result"] == "100.00"
    assert cross["data"]["is_cross_rate"] is True
    assert len(cross["data"]["rates_used"]) == 2
    assert [p["dataset"] for p in cross["provenance"]] == ["fx_reference_rates"]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("amount=0&from=USD&to=INR&date=2026-09-24", "VALIDATION_ERROR"),
        ("amount=-1&from=USD&to=INR&date=2026-09-24", "VALIDATION_ERROR"),
        ("amount=1.234&from=USD&to=INR&date=2026-09-24", "VALIDATION_ERROR"),
        ("amount=1&from=USD&to=USD&date=2026-09-24", "VALIDATION_ERROR"),
        ("amount=1&from=XYZ&to=INR&date=2026-09-24", "INVALID_REQUEST"),
        ("amount=1&from=USD&to=XYZ&date=2026-09-24", "INVALID_REQUEST"),
        ("amount=abc&from=USD&to=INR&date=2026-09-24", "INVALID_REQUEST"),
        ("amount=1e5&from=USD&to=INR&date=2026-09-24", "INVALID_REQUEST"),
        ("amount=NaN&from=USD&to=INR&date=2026-09-24", "INVALID_REQUEST"),
        ("amount=1&from=US&to=INR&date=2026-09-24", "INVALID_REQUEST"),
        ("amount=1&from=USD&to=INR", "INVALID_REQUEST"),
        ("amount=1&from=USD&to=INR&date=2015-01-05", "RATE_NOT_FOUND"),
    ],
)
def test_convert_errors(client: TestClient, query: str, code: str) -> None:
    status = 404 if code == "RATE_NOT_FOUND" else 422
    assert_error(client.get(f"{CONVERT}?{query}"), status, code)


# ---------------------------------------------------------------- stats
STATS = "/v1/fx/stats"


def test_stats_monthly(client: TestClient) -> None:
    parsed = body(client.get(f"{STATS}?currency=USD&from=2026-09-01&to=2026-09-30&source=fbil"))

    (month,) = parsed["data"]
    assert month["period_start"] == "2026-09-01"
    assert month["period_end"] == "2026-09-30"
    assert month["count"] == 17
    assert all(isinstance(month[k], str) for k in ("mean", "min", "max", "first", "last"))
    assert isinstance(month["volatility"], str)
    assert_no_floats(parsed)


def test_stats_weekly_buckets_are_iso_weeks(client: TestClient) -> None:
    parsed = body(
        client.get(f"{STATS}?currency=USD&from=2026-09-01&to=2026-09-30&period=week&source=fbil")
    )

    assert parsed["data"][0]["period_start"] == "2026-08-31"
    assert parsed["meta"]["count"] == len(parsed["data"]) >= 4


def test_stats_without_data_warns(client: TestClient) -> None:
    parsed = body(client.get(f"{STATS}?currency=USD&from=2010-01-01&to=2010-01-31"))

    assert parsed["data"] == []
    assert parsed["meta"]["warnings"] == ["no rates in the requested range"]


def test_stats_validation(client: TestClient) -> None:
    assert_error(
        client.get(f"{STATS}?currency=USD&from=2026-09-01&to=2026-09-30&period=year"),
        422,
        "INVALID_REQUEST",
    )
    assert_error(
        client.get(f"{STATS}?currency=USD&from=2026-09-30&to=2026-09-01"), 422, "VALIDATION_ERROR"
    )
    assert_error(
        client.get(f"{STATS}?currency=USD&from=2000-01-01&to=2026-09-01"), 422, "RANGE_TOO_LARGE"
    )


# ---------------------------------------------------------------- compare
COMPARE = "/v1/fx/compare"


def test_compare_overlap_and_summary(client: TestClient) -> None:
    parsed = body(client.get(f"{COMPARE}?currency=USD&from=2018-07-01&to=2018-07-31"))

    data = parsed["data"]
    assert data["currency"] == "USD"
    assert (data["from"], data["to"]) == ("2018-07-01", "2018-07-31")
    (row,) = data["rows"]
    assert row["date"] == "2018-07-24"
    assert all(isinstance(row[k], str) for k in ("rbi", "fbil", "diff", "diff_bps"))
    assert isinstance(row["flagged"], bool)
    assert data["summary"]["overlap_days"] == 1
    assert data["summary"]["rbi_only_days"] == 6
    assert data["summary"]["fbil_only_days"] == 15
    assert isinstance(data["summary"]["max_abs_diff_bps"], str)
    assert {p["source"] for p in parsed["provenance"]} == {"rbi", "fbil"}
    assert parsed["meta"]["count"] == 1
    assert_no_floats(parsed)


def test_compare_validation(client: TestClient) -> None:
    assert_error(
        client.get(f"{COMPARE}?currency=USD&from=2018-07-31&to=2018-07-01"), 422, "VALIDATION_ERROR"
    )
    assert_error(
        client.get(f"{COMPARE}?currency=USD&from=2000-01-01&to=2026-07-01"), 422, "RANGE_TOO_LARGE"
    )
    assert_error(
        client.get(f"{COMPARE}?currency=ZZZ&from=2018-07-01&to=2018-07-31"), 422, "INVALID_REQUEST"
    )
