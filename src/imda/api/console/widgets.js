// Shared result widgets: loading and empty states, API errors, trust banners and provenance.
import { el, clear, setBusy } from "./dom.js";
import { formatStamp } from "./format.js";

export function showLoading(out, label = "Loading") {
  setBusy(out, true);
  clear(out).append(el("p", { class: "state state-loading", role: "status" }, `${label}…`));
}

export function showEmpty(out, message) {
  setBusy(out, false);
  clear(out).append(el("p", { class: "state state-empty" }, message));
}

export function errorBox(error) {
  const code = error?.code ?? "ERROR";
  const box = el("div", { class: "error", role: "alert" },
    el("p", { class: "error-code" }, code),
    el("p", { class: "error-message" }, error?.message ?? "Something went wrong."));
  const details = error?.details ?? {};
  if (typeof details.hint === "string") {
    box.append(el("p", { class: "error-hint" }, details.hint));
  }
  if (Array.isArray(details.errors) && details.errors.length > 0) {
    const list = el("ul");
    for (const item of details.errors) {
      const where = [item.loc, item.field, item.name].find((v) => typeof v === "string") ?? "";
      const text = [where, item.message ?? item.msg].filter(Boolean).join(": ");
      list.append(el("li", null, text || JSON.stringify(item)));
    }
    box.append(list);
  }
  const meta = [error?.status ? `HTTP ${error.status}` : null, error?.requestId ? `request ${error.requestId}` : null].filter(Boolean);
  if (meta.length) box.append(el("p", { class: "error-meta" }, meta.join(" · ")));
  return box;
}

export function showError(out, error) {
  setBusy(out, false);
  clear(out).append(errorBox(error));
}

/** Merge several envelopes into one view of provenance, warnings and flags. */
function merge(envelopes) {
  const provenance = new Map();
  const warnings = new Set();
  let degraded = false;
  for (const env of envelopes.filter(Boolean)) {
    degraded ||= Boolean(env.meta?.degraded);
    for (const w of env.meta?.warnings ?? []) warnings.add(w);
    for (const p of env.provenance ?? []) {
      const key = `${p.source}/${p.dataset}`;
      const known = provenance.get(key);
      provenance.set(key, known ? { ...p, stale: known.stale || p.stale } : p);
    }
  }
  return { provenance: [...provenance.values()], warnings: [...warnings], degraded };
}

/** A banner when meta says the answer is degraded or stale (or carries other warnings). */
export function trustBanner(envelopes) {
  const { provenance, warnings, degraded } = merge(envelopes);
  const stale = provenance.some((p) => p.stale);
  if (!degraded && !stale && warnings.length === 0) return null;
  const kind = degraded ? "degraded" : stale ? "stale" : "note";
  const title = degraded ? "Degraded" : stale ? "Stale" : "Note";
  const lead = degraded ? "A source is unhealthy; the last good data is served." : stale ? "Some data is behind its expected date." : null;
  const list = el("ul");
  if (lead) list.append(el("li", null, lead));
  for (const w of warnings) list.append(el("li", null, w));
  return el("div", { class: `banner banner-${kind}`, role: "status" }, el("strong", null, title), list);
}

export function provenanceFooter(envelopes) {
  const { provenance } = merge(envelopes);
  const foot = el("footer", { class: "provenance" }, el("span", null, "Provenance"));
  if (provenance.length === 0) {
    foot.append(el("p", null, "No upstream source was used for this answer."));
    return foot;
  }
  const list = el("ul");
  for (const p of provenance) {
    list.append(el("li", null,
      el("span", { class: "src" }, `${p.source}/${p.dataset}`),
      " · fetched ",
      p.fetched_at ? formatStamp(p.fetched_at) : "unknown",
      p.stale ? el("span", { class: "tag-stale" }, " · stale") : null));
  }
  foot.append(list);
  return foot;
}

/** Render `content` with the trust banner above and the provenance footer below. */
export function showResult(out, content, envelopes) {
  setBusy(out, false);
  clear(out);
  const banner = trustBanner(envelopes);
  if (banner) out.append(banner);
  out.append(content, provenanceFooter(envelopes));
}

export function statusPill(status, label) {
  const key = ["ok", "stale", "degraded", "broken"].includes(status) ? status : "unknown";
  return el("span", { class: `pill pill-${key}` }, label ?? status ?? "unknown");
}
