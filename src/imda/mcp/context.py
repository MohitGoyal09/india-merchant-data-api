"""Per-call plumbing: a read-only store, the cached calendar snapshot, and result assembly."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp_types import CallToolResult, TextContent

from imda.api.deps import RequestContext, SnapshotCache
from imda.api.envelope import Used, assess
from imda.api.serialize import to_jsonable
from imda.config import Settings
from imda.domain.fx_service import FxService
from imda.mcp.errors import office_not_found, result_from_exception
from imda.mcp.params import normalise_office
from imda.mcp.sanitize import clean_strings, clean_text
from imda.mcp.schemas import ToolResult
from imda.models import IST
from imda.store.repo import Store

FIXED_NOW_ENV = "IMDA_MCP_FIXED_NOW"
"""Test and eval only: an ISO 8601 datetime with offset that replaces the real clock."""
MAX_TEXT_BYTES = 48_000
"""No tool result carries more text than this (about 50 KB)."""
SUMMARY_CHARS = 1500
_TOO_LARGE_NOTE = (
    "The full data was left out of this text because it is too large; "
    "read it from structuredContent, or narrow the request."
)


def _real_now() -> dt.datetime:
    return dt.datetime.now(IST)


def resolve_now(
    now: Callable[[], dt.datetime] | None = None, environ: Mapping[str, str] | None = None
) -> Callable[[], dt.datetime]:
    """An explicit ``now`` wins, then ``IMDA_MCP_FIXED_NOW`` (test/eval only), then the clock."""
    if now is not None:
        return now
    raw = (os.environ if environ is None else environ).get(FIXED_NOW_ENV, "").strip()
    if not raw:
        return _real_now
    try:
        fixed = dt.datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{FIXED_NOW_ENV} must be an ISO 8601 datetime with offset") from None
    if fixed.tzinfo is None or fixed.utcoffset() is None:
        raise ValueError(f"{FIXED_NOW_ENV} must include a UTC offset, e.g. +05:30")
    return lambda: fixed


@dataclass
class ToolEnv:
    """Everything a tool needs, built once per server."""

    settings: Settings
    now: Callable[[], dt.datetime]
    snapshots: SnapshotCache = field(init=False)

    def __post_init__(self) -> None:
        self.snapshots = SnapshotCache(self.settings)

    @contextmanager
    def request(self) -> Iterator[RequestContext]:
        """A read-only store for one call. ``StoreUnavailable`` propagates; the caller maps it."""
        store = Store.open(self.settings.db_path, read_only=True)
        try:
            snapshot = self.snapshots.get(store)
            yield RequestContext(
                store=store,
                settings=self.settings,
                calendar=snapshot.calendar,
                office_slugs=snapshot.office_slugs,
                loaded_years=snapshot.loaded_years,
                fx=FxService(
                    store, calendar=snapshot.calendar, settings=self.settings, now=self.now
                ),
                now=self.now,
            )
        finally:
            store.close()


def require_office(rc: RequestContext, raw: str) -> str:
    slug = normalise_office(raw)
    if slug not in rc.office_slugs:
        raise office_not_found(slug, rc.office_slugs)
    return slug


@dataclass(frozen=True, slots=True)
class Draft:
    """What a tool handler returns: plain data for the result model, plus a summary line."""

    data: Mapping[str, object]
    summary: str
    used: Sequence[Used] = ()
    warnings: Sequence[str] = ()


Handler = Callable[[RequestContext], Draft]


def _render(summary: str, warnings: Sequence[str], structured: dict[str, Any]) -> str:
    lines = [summary, *(f"WARNING: {w}" for w in warnings)]
    head = "\n".join(lines)
    body = json.dumps(structured, separators=(",", ":"), ensure_ascii=False)
    text = f"{head}\n\n{body}"
    if len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
        return text
    return f"{head}\n\n{_TOO_LARGE_NOTE}"


def run_tool(env: ToolEnv, result_type: type[ToolResult], handler: Handler) -> CallToolResult:
    """Run ``handler`` against a fresh read-only context and wrap the outcome.

    Success: a text summary plus the JSON, and ``structuredContent`` matching ``result_type``.
    Any exception becomes an ``isError`` result; nothing is raised to the host.
    """
    try:
        with env.request() as rc:
            draft = handler(rc)
            assessment = assess(rc, draft.used)
        warnings = list(dict.fromkeys([*draft.warnings, *assessment.warnings]))
        body = clean_strings(
            to_jsonable({**draft.data, "provenance": assessment.provenance, "warnings": warnings})
        )
        structured = result_type.model_validate(body).model_dump(mode="json")
        text = _render(clean_text(draft.summary, SUMMARY_CHARS), warnings, structured)
    except Exception as exc:
        return result_from_exception(exc)
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=structured
    )


def resource_text(result: CallToolResult) -> str:
    """The JSON of a tool result, for a resource: the data on success, the error body otherwise."""
    if result.is_error or result.structured_content is None:
        first = result.content[0]
        return first.text if isinstance(first, TextContent) else "{}"
    return json.dumps(result.structured_content, separators=(",", ":"), ensure_ascii=False)
