"""Rates toolset: FBIL overnight MIBOR."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from imda.api.deps import RequestContext
from imda.api.envelope import Used
from imda.api.routes.mibor import OVERNIGHT_TENORS
from imda.api.serialize import mibor_view
from imda.domain.fx_service import check_range
from imda.errors import RangeTooLarge
from imda.mcp.context import Draft, ToolEnv, run_tool
from imda.mcp.params import parse_range
from imda.mcp.schemas import MiborResult
from imda.mcp.tools._common import DATE_NOTE, READ_ONLY
from imda.models import Dataset, Source

MAX_MIBOR_DAYS = 366
MIBOR_DESCRIPTION = (
    "FBIL overnight MIBOR (Mumbai Interbank Offered Rate), one row per published date, in "
    "percent per annum. Use it for 'what was overnight MIBOR last week?'. On Fridays FBIL "
    "publishes tenor '3D' (Friday to Monday) instead of 'O/N'; both are returned with their "
    "real `tenor` and `spans_weekend`. Only overnight tenors are offered, not term MIBOR. "
    f"Ranges are limited to {MAX_MIBOR_DAYS} days (RANGE_TOO_LARGE otherwise). " + DATE_NOTE
)


def _mibor(rc: RequestContext, from_date: str, to_date: str) -> Draft:
    start, end = parse_range("from_date", from_date, "to_date", to_date)
    check_range(start, end)
    days = (end - start).days
    if days > MAX_MIBOR_DAYS:
        raise RangeTooLarge(days, MAX_MIBOR_DAYS)
    rows = [r for r in rc.store.mibor(start, end) if r.tenor in OVERNIGHT_TENORS]
    latest = rc.store.latest_mibor_date()
    summary = f"{len(rows)} overnight MIBOR rows from {start} to {end}."
    if rows:
        newest = rows[-1]
        summary += f" Latest: {newest.date} {newest.tenor} {newest.rate:f}% p.a."
    return Draft(
        data={
            "from_date": start,
            "to_date": end,
            "count": len(rows),
            "rates": [mibor_view(r) for r in rows],
        },
        summary=summary,
        used=[Used(Source.FBIL, Dataset.MIBOR, latest)],
    )


def register(server: MCPServer, env: ToolEnv) -> None:
    @server.tool(
        name="fetch_mibor",
        title="FBIL overnight MIBOR",
        description=MIBOR_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_mibor(
        from_date: Annotated[str, Field(description="First date, e.g. '2026-09-01'.")],
        to_date: Annotated[str, Field(description="Last date, e.g. '2026-09-30'.")],
    ) -> Annotated[CallToolResult, MiborResult]:
        return run_tool(env, MiborResult, lambda rc: _mibor(rc, from_date, to_date))
