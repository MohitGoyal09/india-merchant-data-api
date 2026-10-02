"""Error envelope and exception handlers. Responses never carry stack traces."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from imda.api.serialize import to_jsonable
from imda.domain.calendar import CalendarDataMissing
from imda.domain.fx_service import RateNotFound

logger = logging.getLogger("imda.api")

UNKNOWN_REQUEST_ID = "unknown"
_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
_BODY_LOC_SKIP = {"body", "query", "path", "header"}


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorEnvelope(BaseModel):
    """Shape of every error response."""

    error: ErrorBody
    request_id: str


class ApiError(Exception):
    """An error with a stable machine code and an HTTP status."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.headers = dict(headers or {})


def office_not_found(slug: str) -> ApiError:
    return ApiError(404, "OFFICE_NOT_FOUND", f"Unknown RBI office {slug!r}", {"office": slug})


def range_too_large(days: int, limit: int) -> ApiError:
    return ApiError(
        422,
        "RANGE_TOO_LARGE",
        f"Range of {days} days exceeds the {limit}-day limit",
        {"days": days, "max_days": limit},
    )


def request_id_of(request: Request) -> str:
    return str(getattr(request.state, "request_id", UNKNOWN_REQUEST_ID))


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {"code": code, "message": message, "details": to_jsonable(dict(details or {}))},
        "request_id": request_id_of(request),
    }
    return JSONResponse(status_code=status_code, content=body, headers=dict(headers or {}))


def _field_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, object]]:
    """Reduce pydantic errors to location, message and type: no input echo, no exception ctx."""
    return [
        {
            "field": ".".join(str(p) for p in err["loc"] if p not in _BODY_LOC_SKIP),
            "in": str(err["loc"][0]) if err["loc"] else "",
            "message": str(err["msg"]),
            "type": str(err["type"]),
        }
        for err in errors
    ]


def _on_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(
        request, exc.status_code, exc.code, exc.message, exc.details, headers=exc.headers
    )


def _on_request_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        request,
        422,
        "INVALID_REQUEST",
        "Request validation failed",
        {"errors": _field_errors(exc.errors())},
    )


def _on_value_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(request, 422, "VALIDATION_ERROR", str(exc))


def _on_rate_not_found(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateNotFound)
    return error_response(
        request,
        404,
        "RATE_NOT_FOUND",
        str(exc),
        {"currency": exc.currency.value, "date": exc.day.isoformat()},
    )


def _on_calendar_missing(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CalendarDataMissing)
    hint = f"imda backfill --datasets holidays --from {exc.year}-01-01"
    return error_response(
        request,
        409,
        "CALENDAR_DATA_MISSING",
        str(exc),
        {"office": exc.office, "year": exc.year, "hint": hint},
    )


def _on_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
    return error_response(
        request, exc.status_code, code, str(exc.detail), headers=getattr(exc, "headers", None)
    )


def internal_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Generic 500. The exception is logged server-side with the request id, never returned."""
    logger.error(
        "unhandled exception request_id=%s",
        request_id_of(request),
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return error_response(request, 500, "INTERNAL_ERROR", "Internal server error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _on_api_error)
    app.add_exception_handler(RequestValidationError, _on_request_validation)
    app.add_exception_handler(RateNotFound, _on_rate_not_found)
    app.add_exception_handler(CalendarDataMissing, _on_calendar_missing)
    app.add_exception_handler(ValueError, _on_value_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_exception)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorEnvelope, "description": "Unknown office, route or rate"},
    409: {"model": ErrorEnvelope, "description": "Holiday data for that year is not loaded"},
    422: {"model": ErrorEnvelope, "description": "Invalid request"},
    500: {"model": ErrorEnvelope, "description": "Unexpected server error"},
}
