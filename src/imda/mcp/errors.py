"""Errors are tool results (``isError=true``) with a JSON body ``{code, message, hint}``.

The codes are the REST API's. Nothing here raises to the host: every exception becomes a result
the model can read and recover from.
"""

from __future__ import annotations

import difflib
import json
import logging
from collections.abc import Iterable

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent
from pydantic import ValidationError

from imda.domain.calendar import CalendarDataMissing
from imda.domain.fx_service import RateNotFound
from imda.errors import InvalidInput, RangeTooLarge
from imda.mcp.sanitize import clean_text
from imda.store.repo import StoreUnavailable

logger = logging.getLogger("imda.mcp")

VALIDATION_ERROR = "VALIDATION_ERROR"
INVALID_REQUEST = "INVALID_REQUEST"
OFFICE_NOT_FOUND = "OFFICE_NOT_FOUND"
RATE_NOT_FOUND = "RATE_NOT_FOUND"
CALENDAR_DATA_MISSING = "CALENDAR_DATA_MISSING"
RANGE_TOO_LARGE = "RANGE_TOO_LARGE"
STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"
ERROR_CODES = (
    VALIDATION_ERROR,
    INVALID_REQUEST,
    OFFICE_NOT_FOUND,
    RATE_NOT_FOUND,
    CALENDAR_DATA_MISSING,
    RANGE_TOO_LARGE,
    STORE_UNAVAILABLE,
    INTERNAL_ERROR,
)
_MAX_FIELD_ERRORS = 5


class McpToolError(Exception):
    """A problem the model can fix or report. ``hint`` says what to do next."""

    def __init__(self, code: str, message: str, hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def invalid_request(message: str, hint: str) -> McpToolError:
    return McpToolError(INVALID_REQUEST, message, hint)


def office_not_found(slug: str, known: Iterable[str]) -> McpToolError:
    close = difflib.get_close_matches(slug, sorted(known), n=3, cutoff=0.6)
    guess = f" Did you mean: {', '.join(close)}?" if close else ""
    return McpToolError(
        OFFICE_NOT_FOUND,
        f"Unknown RBI office {clean_text(slug, 60)!r}",
        f"Call fetch_all_offices for the valid office slugs (for example 'mumbai').{guess}",
    )


def error_result(code: str, message: str, hint: str) -> CallToolResult:
    body = {"code": code, "message": clean_text(message), "hint": clean_text(hint)}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        is_error=True,
    )


def _validation_message(exc: ValidationError) -> str:
    """Field names and rules only: the rejected values are the caller's data."""
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors()[:_MAX_FIELD_ERRORS]
    ]
    return "Invalid arguments. " + "; ".join(parts)


def result_from_tool_error(exc: ToolError) -> CallToolResult:
    """Errors the SDK raises before or around a tool body: bad arguments, unknown tool."""
    cause = exc.__cause__
    if isinstance(cause, ValidationError):
        return error_result(
            INVALID_REQUEST,
            _validation_message(cause),
            "Fix the argument named above and call the tool again. Dates are YYYY-MM-DD.",
        )
    return error_result(
        INVALID_REQUEST,
        str(exc),
        "Use a tool name from the tool list and the documented arguments.",
    )


def result_from_exception(exc: BaseException) -> CallToolResult:
    """Map any exception to an error result. Unknown ones are logged, not shown."""
    if isinstance(exc, McpToolError):
        return error_result(exc.code, exc.message, exc.hint)
    if isinstance(exc, ValidationError):
        return error_result(
            INVALID_REQUEST,
            _validation_message(exc),
            "Fix the argument named above and call the tool again.",
        )
    if isinstance(exc, RangeTooLarge):
        return error_result(
            RANGE_TOO_LARGE,
            str(exc),
            f"Use a range of at most {exc.limit} days, or split the range into parts.",
        )
    if isinstance(exc, InvalidInput):
        return error_result(
            VALIDATION_ERROR, str(exc), "Correct the argument described above and try again."
        )
    if isinstance(exc, RateNotFound):
        return error_result(
            RATE_NOT_FOUND,
            str(exc),
            "No rate was published recently before that date. Try an earlier date, or call "
            "fetch_all_fx_rates to see which dates have data.",
        )
    if isinstance(exc, CalendarDataMissing):
        return error_result(
            CALENDAR_DATA_MISSING,
            str(exc),
            f"Holiday data for {exc.year} is not loaded for {exc.office}. Tell the user that "
            "year is unavailable. Do not assume there are no holidays.",
        )
    if isinstance(exc, StoreUnavailable):
        return error_result(
            STORE_UNAVAILABLE,
            "The data store is missing or not set up on the server",
            "Tell the user the data service is unavailable. Retrying will not help.",
        )
    logger.error("unhandled exception in MCP tool", exc_info=(type(exc), exc, exc.__traceback__))
    return error_result(
        INTERNAL_ERROR,
        "Internal server error",
        "Retry once. If it fails again, tell the user the data service has a problem.",
    )
