"""Health toolset: how fresh and trustworthy each data source is."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult

from imda.api.deps import RequestContext
from imda.api.envelope import Used
from imda.api.routes.sources import RECENT_RUNS, drift_view, freshness_view, worst_status
from imda.health.freshness import FreshnessReport, assess_freshness
from imda.mcp.context import Draft, ToolEnv, resource_text, run_tool
from imda.mcp.schemas import SourceHealthResult
from imda.mcp.tools._common import READ_ONLY, UNTRUSTED_NOTE
from imda.models import Dataset, Source, SourceStatus

_PROVENANCE_PAIRS = frozenset(
    {
        (Source.RBI.value, Dataset.OFFICES.value),
        (Source.RBI.value, Dataset.HOLIDAYS.value),
        (Source.RBI.value, Dataset.FX.value),
        (Source.FBIL.value, Dataset.FX.value),
        (Source.FBIL.value, Dataset.MIBOR.value),
    }
)
HEALTH_DESCRIPTION = (
    "Report the health of every data source (RBI holidays, RBI and FBIL FX rates, FBIL "
    "MIBOR): status ok/degraded/broken/unknown, when it last succeeded, schema drift, and "
    "freshness (latest date held versus the date that should exist). Use it to say how fresh "
    "the data behind an answer is, or when another tool returned a stale or degraded warning. "
    "Takes no arguments. It reads recorded checks; it cannot refresh data or call RBI/FBIL. "
    + UNTRUSTED_NOTE
)


def _item(
    key: tuple[str, str], row: Mapping[str, Any] | None, fresh: FreshnessReport | None
) -> dict[str, object]:
    row = row or {}
    return {
        "source": key[0],
        "dataset": key[1],
        "status": row.get("status", SourceStatus.UNKNOWN.value),
        "checked_at": row.get("checked_at"),
        "last_success_at": row.get("last_success_at"),
        "last_error_at": row.get("last_error_at"),
        "last_error": row.get("last_error"),
        "drift": drift_view(row.get("drift")),
        "freshness": freshness_view(fresh),
    }


def _health(rc: RequestContext) -> Draft:
    rows = {(str(r["source"]), str(r["dataset"])): r for r in rc.store.source_health()}
    reports = assess_freshness(rc.store, rc.calendar, rc.now(), rc.settings)
    fresh = {(r.source.value, r.dataset.value): r for r in reports}
    items = [
        _item(key, rows.get(key), fresh.get(key)) for key in sorted(rows.keys() | fresh.keys())
    ]
    status = worst_status([str(item["status"]) for item in items])
    stale = [
        f"{r.source.value}/{r.dataset.value} is stale: latest data "
        f"{r.latest_date or 'none'}, expected {r.expected_date}"
        for r in reports
        if r.stale
    ]
    runs = [
        {
            "run_id": run["run_id"],
            "kind": run["kind"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        }
        for run in rc.store.runs(RECENT_RUNS)
    ]
    bad = [f"{i['source']}/{i['dataset']} {i['status']}" for i in items if i["status"] != "ok"]
    summary = f"Overall data status: {status}. " + (
        f"Not ok: {', '.join(bad)}." if bad else f"All {len(items)} source datasets are ok."
    )
    used = [Used(Source(s), Dataset(d)) for (s, d) in sorted(rows) if (s, d) in _PROVENANCE_PAIRS]
    return Draft(
        data={"status": status, "sources": items, "last_runs": runs},
        summary=summary,
        used=used,
        warnings=stale,
    )


def register(server: MCPServer, env: ToolEnv) -> None:
    @server.tool(
        name="fetch_source_health",
        title="Data source health and freshness",
        description=HEALTH_DESCRIPTION,
        annotations=READ_ONLY,
    )
    def fetch_source_health() -> Annotated[CallToolResult, SourceHealthResult]:
        return run_tool(env, SourceHealthResult, _health)

    @server.resource(
        "imda://sources/health",
        name="sources_health",
        title="Data source health",
        description="Status, drift and freshness of every data source, as JSON.",
        mime_type="application/json",
    )
    def health_resource() -> str:
        return resource_text(run_tool(env, SourceHealthResult, _health))
