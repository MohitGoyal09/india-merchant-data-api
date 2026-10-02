// Panel 4: source health, plus the overall status pill in the masthead.
import { $, el, setBusy } from "./dom.js";
import { get } from "./api.js";
import { formatDate, formatStamp, plural } from "./format.js";
import { showError, showLoading, showResult, statusPill } from "./widgets.js";

const LABELS = { ok: "OK", stale: "Stale", degraded: "Degraded", broken: "Broken", unknown: "Unknown" };

export function overallStatus(data) {
  if (data.status !== "ok") return data.status;
  return data.sources.some((s) => s.freshness?.stale) ? "stale" : "ok";
}

function setOverall(status) {
  const known = status in LABELS ? status : "unknown";
  const pill = $("#overall-pill");
  pill.setAttribute("class", `pill pill-${known}`);
  pill.textContent = LABELS[known];
}

function freshnessCell(fresh) {
  if (!fresh) return el("td", null, "Reference data (no daily series)");
  const lag = fresh.lag_business_days;
  return el("td", null,
    el("span", { class: "num" }, fresh.latest_date ? formatDate(fresh.latest_date) : "no data"),
    ` latest, ${fresh.expected_date ? formatDate(fresh.expected_date) : "?"} expected`,
    el("br"),
    `${plural(lag, "business day")} behind`,
    fresh.calendar_incomplete ? " (holiday calendar incomplete)" : "");
}

function driftCell(drift) {
  if (!drift) return el("td", null, "None recorded");
  return el("td", null, drift.drifted ? el("strong", null, "Drift: ") : "No drift. ", drift.summary || drift.note || "");
}

function sourceRow(item) {
  const status = el("td", null, statusPill(item.status, LABELS[item.status] ?? item.status));
  if (item.freshness?.stale) status.append(" ", statusPill("stale", "Stale"));
  if (item.last_error) status.append(el("br"), el("span", { class: "hint" }, `Last error: ${item.last_error}`));
  return el("tr", null,
    el("th", { scope: "row" }, el("span", { class: "num" }, `${item.source}/${item.dataset}`)),
    status,
    el("td", null, formatStamp(item.last_success_at)),
    freshnessCell(item.freshness),
    driftCell(item.drift));
}

export function renderHealth(data) {
  const head = el("tr", null, ...["Source", "Status", "Last success", "Freshness", "Drift"].map((t) => el("th", { scope: "col" }, t)));
  const table = el("table", null,
    el("caption", null, "Upstream sources"),
    el("thead", null, head),
    el("tbody", null, ...data.sources.map(sourceRow)));
  const root = el("div", { class: "stack" }, el("div", { class: "table-scroll" }, table));
  if (data.last_runs.length > 0) {
    const list = el("ul");
    for (const run of data.last_runs) {
      list.append(el("li", null, `${run.kind} · ${run.status} · started ${formatStamp(run.started_at)}`));
    }
    root.append(el("details", null, el("summary", null, `Last ${plural(data.last_runs.length, "ingest run")}`), list));
  }
  return root;
}

export function initHealth() {
  const out = $("#health-out");
  const button = $("#health-refresh");

  async function run() {
    button.disabled = true;
    showLoading(out, "Checking sources");
    try {
      const envelope = await get("/v1/sources/health", {});
      setOverall(overallStatus(envelope.data));
      showResult(out, renderHealth(envelope.data), [envelope]);
      return envelope.data;
    } catch (error) {
      setOverall("unknown");
      showError(out, error);
      return null;
    } finally {
      button.disabled = false;
      setBusy(out, false);
    }
  }

  button.addEventListener("click", run);
  return { run };
}
