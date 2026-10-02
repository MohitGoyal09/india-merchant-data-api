// Merchant Console entry point: wires the four panels to the same-origin REST API.
import { $, el } from "./dom.js";
import { get } from "./api.js";
import { addDays, nowIstLocal } from "./format.js";
import { initFx } from "./fx.js";
import { initHealth } from "./health.js";
import { initInvoice } from "./invoice.js";
import { initSettlement } from "./settlement.js";
import { showError } from "./widgets.js";

const DEFAULT_OFFICE = "mumbai";
const DEFAULT_RANGE_DAYS = 90;

async function loadOffices() {
  const selects = [$("#eta-office"), $("#quote-office")];
  try {
    const { data } = await get("/v1/offices", {});
    for (const select of selects) {
      select.replaceChildren(...data.map((o) => el("option", { value: o.slug }, `${o.name}, ${o.state}`)));
      select.value = DEFAULT_OFFICE;
    }
  } catch (error) {
    for (const select of selects) {
      select.replaceChildren(el("option", { value: "" }, "Offices unavailable"));
    }
    showError($("#eta-out"), error);
  }
}

/** Newest data date across the FX series, used to seed sensible defaults for a demo database. */
function latestFxDate(health) {
  const dates = (health?.sources ?? [])
    .filter((s) => s.dataset.startsWith("fx"))
    .map((s) => s.freshness?.latest_date)
    .filter(Boolean)
    .sort();
  return dates.at(-1) ?? null;
}

function seedDefaults(latest, controls) {
  const day = latest ?? nowIstLocal().slice(0, 10);
  if (!controls.captured.dataset.touched) controls.captured.value = `${day}T11:00`;
  if (!controls.invoiceDate.dataset.touched) controls.invoiceDate.value = day;
  controls.fx.setRange(addDays(day, 1 - DEFAULT_RANGE_DAYS), day);
}

async function main() {
  const captured = $("#eta-captured");
  const invoiceDate = $("#quote-date");
  for (const input of [captured, invoiceDate]) {
    input.value = input.type === "date" ? nowIstLocal().slice(0, 10) : nowIstLocal();
    input.addEventListener("input", () => { input.dataset.touched = "1"; });
  }

  const offices = loadOffices();
  const health = initHealth();
  const settlement = initSettlement({ ready: offices });
  const invoice = initInvoice();
  const fx = initFx();

  const [latest] = await Promise.all([health.run().then(latestFxDate), offices]);
  seedDefaults(latest, { captured, invoiceDate, fx });
  settlement.run();
  invoice.run();
  fx.run();
}

main();
