// Panel 3: FX explorer. Rates (all pages), month stats, RBI/FBIL compare and Mumbai holidays.
import { $, el } from "./dom.js";
import { get } from "./api.js";
import { createChart } from "./chart.js";
import { formatDate, formatDateLong, formatMonthYear, plural, decimalPlaces } from "./format.js";
import { errorBox, showError, showLoading, showResult } from "./widgets.js";

const MAX_PAGES = 12;
const PAGE_LIMIT = 1000;
const HOLIDAY_OFFICE = "mumbai";

async function fetchAllRates(params) {
  const envelopes = [];
  let cursor;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const envelope = await get("/v1/fx/rates", { ...params, limit: PAGE_LIMIT, cursor });
    envelopes.push(envelope);
    cursor = envelope.meta.next_cursor;
    if (!cursor) break;
  }
  return envelopes;
}

async function fetchHolidays(from, to) {
  const years = [];
  for (let y = Number(from.slice(0, 4)); y <= Number(to.slice(0, 4)); y += 1) years.push(y);
  const settled = await Promise.allSettled(years.map((year) => get("/v1/holidays", { office: HOLIDAY_OFFICE, year })));
  const holidays = [];
  const envelopes = [];
  const missing = [];
  settled.forEach((result, i) => {
    if (result.status === "fulfilled") {
      envelopes.push(result.value);
      for (const h of result.value.data) if (h.date >= from && h.date <= to) holidays.push(h);
    } else {
      missing.push({ year: years[i], error: result.reason });
    }
  });
  holidays.sort((a, b) => a.date.localeCompare(b.date));
  return { holidays, envelopes, missing };
}

function compareLine(report) {
  const s = report.summary;
  if (!s || s.overlap_days === 0) {
    return "No days in this range have both an RBI and an FBIL rate to compare.";
  }
  const flagged = s.flagged_days === 0 ? "none flagged above 1 bp" : `${plural(s.flagged_days, "day")} flagged above 1 bp`;
  return `RBI and FBIL differ by max ${s.max_abs_diff_bps} bps over ${plural(s.overlap_days, "day")}; ${flagged}.`;
}

function statsTable(rows) {
  const cols = [["Month", false], ["Days", true], ["Mean", true], ["Min", true], ["Max", true], ["First", true], ["Last", true], ["Change %", true], ["Volatility %", true]];
  const head = el("tr", null, ...cols.map(([t, num]) => el("th", { scope: "col", class: num ? "num" : null }, t)));
  const body = rows.map((r) => el("tr", null,
    el("th", { scope: "row" }, formatMonthYear(r.period_start)),
    ...[String(r.count), ...[r.mean, r.min, r.max, r.first, r.last].map((v) => decimalPlaces(v, 4)), decimalPlaces(r.change_pct, 2), decimalPlaces(r.volatility, 3)].map((v) => el("td", { class: "num" }, v))));
  return el("div", { class: "table-scroll" },
    el("table", null, el("caption", null, "By month (rate per unit, INR)"), el("thead", null, head), el("tbody", null, ...body)));
}

function holidayList(holidays, missing) {
  const items = el("ul");
  for (const h of holidays) items.append(el("li", null, el("span", { class: "num" }, h.date), `  ${h.name}`));
  const box = el("details", null,
    el("summary", null, `Mumbai bank holidays in range (${holidays.length})`),
    holidays.length ? items : el("p", null, "None in this range."));
  for (const m of missing) {
    box.append(el("p", { class: "hint" }, `Holiday ticks unavailable for ${m.year}: ${m.error?.code ?? "error"}${m.error?.details?.hint ? ` (${m.error.details.hint})` : ""}`));
  }
  return box;
}

function describePoint({ point, index, holiday }, points, holidays, readout) {
  readout.replaceChildren();
  if (holiday) {
    readout.append("Mumbai bank holiday ", el("strong", null, formatDate(holiday.date)), `: ${holiday.name}`);
    return;
  }
  readout.append(el("strong", null, formatDate(point.date)), `  ₹${point.text} per unit · ${point.source.toUpperCase()}`);
  const prev = index > 0 ? points[index - 1].date : null;
  const between = prev ? holidays.filter((h) => h.date > prev && h.date < point.date) : [];
  if (between.length) {
    const names = between.slice(0, 3).map((h) => `${formatDate(h.date)} ${h.name}`);
    const more = between.length > 3 ? ` and ${between.length - 3} more` : "";
    readout.append(el("br"), `No rate between ${formatDate(prev)} and here: ${names.join("; ")}${more}.`);
  }
}

function renderSeries({ rows, currency, holidays, stats, compare, subErrors, missing }) {
  const points = rows.map((r) => ({ date: r.date, value: Number(r.rate_per_unit), text: r.rate_per_unit, source: r.source }));
  const last = rows[rows.length - 1];
  const first = rows[0];
  const readout = el("p", { class: "readout", "aria-live": "polite" },
    "Hover the chart or focus it and use the arrow keys to read a day.");
  const chart = createChart({
    points, holidays, label: `${currency} to INR reference rate per unit`,
    onActive: (info) => describePoint(info, points, holidays, readout),
  });
  const root = el("div", { class: "stack" });
  root.append(el("div", null,
    el("p", { class: "hero-kicker" }, `${currency} in rupees, latest in range`),
    el("p", { class: "hero-figure" }, `₹${last.rate_per_unit}`),
    el("p", { class: "hero-sub" },
      `${formatDateLong(last.date)}, ${last.source.toUpperCase()}. ${plural(rows.length, "observation")} from ${formatDate(first.date)}; first value ₹${first.rate_per_unit}.`)));
  root.append(el("div", null, chart.node, readout,
    el("p", { class: "hint" }, el("span", { class: "holiday-key" }), "Mumbai bank holiday")));
  if (compare) root.append(el("p", { class: "compare-line" }, compareLine(compare)));
  if (stats) root.append(stats.length ? statsTable(stats) : el("p", { class: "state" }, "No monthly statistics for this range."));
  for (const error of subErrors) root.append(el("div", { class: "sub-error" }, errorBox(error)));
  root.append(holidayList(holidays, missing));
  return { root, chart };
}

export function initFx() {
  const form = $("#fx-form");
  const out = $("#fx-out");
  const currency = $("#fx-currency");
  const from = $("#fx-from");
  const to = $("#fx-to");
  let chartHandle = null;

  async function run() {
    chartHandle?.destroy();
    for (const input of [from, to]) {
      if (input.value) input.removeAttribute("aria-invalid");
      else input.setAttribute("aria-invalid", "true");
    }
    if (!from.value || !to.value) {
      showError(out, { code: "VALIDATION_ERROR", message: "Choose both a start and an end date." });
      (from.value ? to : from).focus();
      return;
    }
    const range = { currency: currency.value, from: from.value, to: to.value };
    showLoading(out, "Loading rates");
    const [rates, stats, compare, holidays] = await Promise.allSettled([
      fetchAllRates(range),
      get("/v1/fx/stats", { ...range, period: "month" }),
      get("/v1/fx/compare", range),
      fetchHolidays(range.from, range.to),
    ]);
    if (rates.status === "rejected") {
      showError(out, rates.reason);
      return;
    }
    const pages = rates.value;
    const rows = pages.flatMap((p) => p.data);
    const held = holidays.status === "fulfilled" ? holidays.value : { holidays: [], envelopes: [], missing: [] };
    const envelopes = [...pages, ...[stats, compare].filter((r) => r.status === "fulfilled").map((r) => r.value), ...held.envelopes];
    if (rows.length === 0) {
      showResult(out, el("p", { class: "state state-empty" }, `No ${range.currency} rates were published between ${formatDate(range.from)} and ${formatDate(range.to)}.`), envelopes);
      return;
    }
    const subErrors = [stats, compare].filter((r) => r.status === "rejected").map((r) => r.reason);
    const view = renderSeries({
      rows, currency: range.currency, holidays: held.holidays,
      stats: stats.status === "fulfilled" ? stats.value.data : null,
      compare: compare.status === "fulfilled" ? compare.value.data : null,
      subErrors, missing: held.missing,
    });
    showResult(out, view.root, envelopes);
    chartHandle = view.chart;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run();
  });
  $("#fx-sept").addEventListener("click", () => {
    currency.value = "USD";
    from.value = "2026-09-01";
    to.value = "2026-09-30";
    run();
  });
  return { run, setRange: (a, b) => { from.value = a; to.value = b; } };
}
