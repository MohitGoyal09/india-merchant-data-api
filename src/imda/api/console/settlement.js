// Panel 1: settlement ETA with a 14-day strip of counted and skipped days.
import { $, el } from "./dom.js";
import { get } from "./api.js";
import { addDays, formatCaptured, formatDate, formatDateLong, formatDayMonth, localToIso, weekday } from "./format.js";
import { showError, showLoading, showResult } from "./widgets.js";

const STRIP_DAYS = 14;
const MODE_TEXT = {
  working_days: (n) => `${n} working ${n === 1 ? "day" : "days"} after capture`,
  calendar_then_roll: (n) => `${n} calendar ${n === 1 ? "day" : "days"} after capture, rolled forward to an open day`,
};

function classifyDay(data, iso, index) {
  const counted = new Map(data.counted_days.map((d, i) => [d, i + 1]));
  const skipped = new Map(data.skipped.map((s) => [s.date, s.reason]));
  if (iso === data.eta_date) {
    return { kind: "eta", tag: "ETA", words: "estimated settlement date", counted: counted.get(iso) };
  }
  if (index === 0) {
    const reason = skipped.get(iso);
    return { kind: "capture", tag: "Start", words: reason ? `capture day, closed: ${reason}` : "capture day" };
  }
  if (counted.has(iso)) return { kind: "counted", tag: `Day ${counted.get(iso)}`, words: `counted day ${counted.get(iso)}` };
  if (skipped.has(iso)) return { kind: "skipped", tag: "Skip", words: `skipped: ${skipped.get(iso)}`, reason: skipped.get(iso) };
  if (iso < data.eta_date) return { kind: "calendar", tag: `+${index}`, words: `calendar day ${index}` };
  return { kind: "after", tag: "", words: "after the ETA" };
}

function dayCell(data, iso, index) {
  const info = classifyDay(data, iso, index);
  const label = `${formatDate(iso)}: ${info.words}`;
  const showMonth = index === 0 || iso.endsWith("-01");
  return el("li", { class: `day day-${info.kind}`, "aria-label": label, title: label },
    el("span", { class: "day-wd" }, weekday(iso)),
    el("span", { class: "day-num" }, String(Number(iso.slice(8)))),
    showMonth ? el("span", { class: "day-mon" }, formatDayMonth(iso).replace(/^\d+\s/, "")) : null,
    info.reason ? el("span", { class: "day-reason" }, info.reason) : null,
    el("span", { class: "day-tag" }, info.tag));
}

export function buildStrip(data) {
  const strip = el("ol", { class: "strip", "aria-label": `Fourteen days from ${formatDate(data.capture_date)}` });
  for (let i = 0; i < STRIP_DAYS; i += 1) strip.append(dayCell(data, addDays(data.capture_date, i), i));
  const legend = el("ul", { class: "legend", "aria-hidden": "true" },
    el("li", null, el("span", { class: "swatch day-capture" }), "Start (capture day)"),
    el("li", null, el("span", { class: "swatch day-counted" }), "Counted"),
    el("li", null, el("span", { class: "swatch day-skipped" }), "Skipped (closed)"),
    el("li", null, el("span", { class: "swatch day-eta" }), "ETA"));
  return [strip, legend];
}

function skippedList(data) {
  if (data.skipped.length === 0) return el("p", { class: "hero-sub" }, "No closed days between capture and the ETA.");
  const list = el("dl");
  for (const s of data.skipped) list.append(el("dt", null, s.date), el("dd", null, s.reason));
  return el("div", { class: "skips" }, el("h3", null, "Skipped days"), list);
}

export function officeName(slug) {
  const option = [...document.querySelectorAll("#eta-office option")].find((o) => o.value === slug);
  return option ? option.textContent.split(",")[0] : slug;
}

export function renderEta(data) {
  const mode = MODE_TEXT[data.mode]?.(data.cycle_days) ?? data.mode;
  return el("div", null,
    el("p", { class: "hero-kicker" }, "Estimated settlement"),
    el("p", { class: "hero-figure", id: "eta-date" }, formatDateLong(data.eta_date)),
    el("p", { class: "hero-sub" },
      `T+${data.cycle_days}: ${mode}. Captured ${formatCaptured(data.captured_at)}, ${officeName(data.office)} office.`),
    ...buildStrip(data),
    skippedList(data),
    el("p", { class: "disclaimer" }, data.disclaimer));
}

export function initSettlement({ ready }) {
  const form = $("#eta-form");
  const out = $("#eta-out");
  const captured = $("#eta-captured");

  async function run() {
    if (!captured.value) {
      captured.setAttribute("aria-invalid", "true");
      showError(out, { code: "VALIDATION_ERROR", message: "Enter the capture date and time." });
      captured.focus();
      return;
    }
    captured.removeAttribute("aria-invalid");
    const data = new FormData(form);
    showLoading(out, "Estimating");
    try {
      const envelope = await get("/v1/settlement/eta", {
        captured_at: localToIso(captured.value),
        office: data.get("office"),
        cycle_days: data.get("cycle"),
        mode: data.get("mode"),
      });
      showResult(out, renderEta(envelope.data), [envelope]);
    } catch (error) {
      showError(out, error);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run();
  });
  $("#eta-example").addEventListener("click", async () => {
    await ready;
    captured.value = "2026-03-27T11:00";
    $("#eta-office").value = "mumbai";
    $("#eta-cycle").value = "2";
    $("#eta-mode-wd").checked = true;
    run();
  });
  return { run };
}
