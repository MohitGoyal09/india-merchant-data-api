"""Shared assertions for API tests."""

from __future__ import annotations

from typing import Any

from httpx import Response


def body(response: Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    parsed: dict[str, Any] = response.json()
    return parsed


def assert_envelope(parsed: dict[str, Any]) -> None:
    assert set(parsed) == {"data", "meta", "provenance"}
    assert set(parsed["meta"]) == {"count", "next_cursor", "degraded", "warnings"}
    assert isinstance(parsed["provenance"], list)


def assert_error(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    parsed: dict[str, Any] = response.json()
    assert set(parsed) == {"error", "request_id"}
    assert parsed["error"]["code"] == code
    assert set(parsed["error"]) == {"code", "message", "details"}
    assert "Traceback" not in response.text
    return parsed
