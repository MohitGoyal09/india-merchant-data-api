"""Hostile SQL strings in query parameters: only 4xx, no data change, no data leak.

Each payload goes into every parameter it could reach (office, year, month, currency, dates,
cursor). The seeded database must hold the same rows afterwards, every table must still exist,
and a valid request must return the same holidays as before.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

PAYLOADS = [
    "' OR 1=1 --",
    "'; DROP TABLE fx_rates; --",
    "mumbai' UNION SELECT slug, name, NULL, NULL FROM offices --",
    "mumbai' UNION SELECT 1,2,3,4,5 --",
    '" OR ""="',
    "2026-01-01' OR '1'='1",
    "1; DELETE FROM holidays; --",
    "USD' OR 1=1 --",
    "%27%20OR%201%3D1%20--",
    "mumbai\x00' OR 1=1 --",
]
VALID_OFFICE = "mumbai"
VALID_HOLIDAYS = f"/v1/holidays?office={VALID_OFFICE}&year=2026"


def _table_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def _cursor_of(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _requests(payload: str) -> list[tuple[str, dict[str, str]]]:
    """(path, params) pairs that put ``payload`` into one parameter at a time."""
    fx = {"currency": "USD", "from": "2026-09-01", "to": "2026-09-30"}
    day = {"date": "2026-03-31", "office": VALID_OFFICE}
    cases: list[tuple[str, dict[str, str]]] = [
        ("/v1/holidays", {"office": payload, "year": "2026"}),
        ("/v1/holidays", {"office": VALID_OFFICE, "year": payload}),
        ("/v1/holidays", {"office": VALID_OFFICE, "year": "2026", "month": payload}),
        ("/v1/calendar/business-day", {**day, "office": payload}),
        ("/v1/calendar/business-day", {**day, "date": payload}),
    ]
    for key in ("currency", "from", "to", "cursor"):
        cases.append(("/v1/fx/rates", {**fx, key: payload}))
    cases.append(("/v1/fx/rates", {**fx, "cursor": _cursor_of(payload)}))
    cases.append(("/v1/fx/rates", {**fx, "cursor": _cursor_of("2026-09-10" + payload)}))
    cases.append(("/v1/fx/rates", {**fx, "source": payload}))
    return cases


def _ids(payload: str) -> str:
    return payload.encode("unicode_escape").decode()[:24]


@pytest.mark.parametrize("payload", PAYLOADS, ids=_ids)
def test_hostile_strings_get_only_4xx_and_change_nothing(
    client: TestClient, db_path: Path, payload: str
) -> None:
    before_rows = _table_counts(db_path)
    before_holidays: dict[str, Any] = client.get(VALID_HOLIDAYS).json()
    assert before_holidays["meta"]["count"] > 0

    outcomes = [
        (path, params, client.get(path, params=params)) for path, params in _requests(payload)
    ]

    for path, params, response in outcomes:
        assert 400 <= response.status_code < 500, (path, params, response.status_code)
        assert "Traceback" not in response.text
        assert "sqlite" not in response.text.lower()
    assert _table_counts(db_path) == before_rows
    assert "fx_rates" in before_rows  # the table the DROP payload names still exists
    assert client.get(VALID_HOLIDAYS).json() == before_holidays


def test_union_payload_never_returns_rows_from_another_table(client: TestClient) -> None:
    payload = "mumbai' UNION SELECT slug, name, NULL, NULL FROM offices --"

    response = client.get("/v1/holidays", params={"office": payload, "year": "2026"})

    assert 400 <= response.status_code < 500
    assert "data" not in response.json()
