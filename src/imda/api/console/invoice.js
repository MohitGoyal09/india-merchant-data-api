// Panel 2: invoice quote (POST /v1/invoice/quote).
import { $, el } from "./dom.js";
import { post } from "./api.js";
import { formatDate, formatDateLong, formatINR, groupWestern, localToIso, plural } from "./format.js";
import { officeName } from "./settlement.js";
import { showError, showLoading, showResult } from "./widgets.js";

function rateLine(step, currency) {
  const rate = step.rate;
  const unit = rate.unit > 1 ? `${rate.unit} ${step.currency}` : step.currency;
  const parts = [`${formatINR(rate.rate)} per ${unit}`, rate.source.toUpperCase(), `dated ${formatDate(step.effective_date)}`];
  const line = el("dd", null, el("span", { class: "num" }, parts[0]), ` · ${parts[1]} · ${parts[2]}`);
  if (step.lag_days > 0) {
    const why = step.reason ? `: ${step.reason}` : "";
    line.append(el("br"), `Requested ${formatDate(step.requested_date)}; used the rate from ${plural(step.lag_days, "day")} earlier${why}.`);
  }
  return line;
}

export function renderQuote(data) {
  const conv = data.conversion;
  const facts = el("dl", { class: "facts" });
  conv.rates_used.forEach((step, index) => {
    const title = conv.is_cross_rate ? `Rate ${index + 1}, ${step.currency}` : "Rate used";
    facts.append(el("div", null, el("dt", null, title), rateLine(step, conv.from)));
  });
  facts.append(el("div", null, el("dt", null, "Unrounded value"), el("dd", { class: "num" }, formatINR(conv.exact))));
  const root = el("div", null,
    el("p", { class: "hero-kicker" }, "Invoice value in rupees"),
    el("p", { class: "hero-figure", id: "quote-inr" }, formatINR(conv.result)),
    el("p", { class: "hero-sub" }, `${groupWestern(conv.amount)} ${conv.from} on ${formatDate(data.invoice_date)}, ${officeName(data.office)} calendar.`),
    facts);
  if (data.settlement) {
    const s = data.settlement;
    root.append(el("dl", { class: "facts" },
      el("div", null, el("dt", null, "Settlement ETA"),
        el("dd", null, el("strong", null, formatDateLong(s.eta_date)),
          el("br"), `T+${s.cycle_days}, ${s.mode === "working_days" ? "working days" : "calendar then roll"}; ${plural(s.skipped.length, "closed day")} skipped.`))));
  }
  if (data.notes.length > 0) {
    const list = el("ul", { class: "notes" });
    for (const note of data.notes) list.append(el("li", null, note));
    root.append(list);
  }
  return root;
}

function markInvalid(input, invalid) {
  if (invalid) input.setAttribute("aria-invalid", "true");
  else input.removeAttribute("aria-invalid");
}

export function initInvoice() {
  const form = $("#quote-form");
  const out = $("#quote-out");

  async function run() {
    const data = new FormData(form);
    const amount = String(data.get("amount") ?? "").trim().replace(/,/g, "");
    const invoiceDate = String(data.get("invoice_date") ?? "");
    const amountInput = $("#quote-amount");
    const dateInput = $("#quote-date");
    markInvalid(amountInput, !amount);
    markInvalid(dateInput, !invoiceDate);
    if (!amount || !invoiceDate) {
      showError(out, { code: "VALIDATION_ERROR", message: "Enter an amount and an invoice date." });
      (amount ? dateInput : amountInput).focus();
      return;
    }
    const body = { amount, currency: data.get("currency"), invoice_date: invoiceDate, office: data.get("office") };
    const captured = String(data.get("captured") ?? "");
    if (captured) body.captured_at = localToIso(captured);
    showLoading(out, "Quoting");
    try {
      const envelope = await post("/v1/invoice/quote", body);
      showResult(out, renderQuote(envelope.data), [envelope]);
    } catch (error) {
      showError(out, error);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run();
  });
  return { run };
}
