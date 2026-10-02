// Formatting. Money stays a decimal string end to end; floats are used only for chart geometry.

const DAY_MS = 86_400_000;
const IST_OFFSET = "+05:30";
const dateFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short", year: "numeric" });
const dateLongFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric" });
const dayMonthFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "numeric", month: "short" });
const monthYearFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", month: "short", year: "numeric" });
const weekdayFmt = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short" });
const stampFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
});
const istParts = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export function parseISODate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

export function toISODate(date) {
  return date.toISOString().slice(0, 10);
}

export function addDays(iso, days) {
  return toISODate(new Date(parseISODate(iso).getTime() + days * DAY_MS));
}

export function daysBetween(fromIso, toIso) {
  return Math.round((parseISODate(toIso) - parseISODate(fromIso)) / DAY_MS);
}

export const formatDate = (iso) => dateFmt.format(parseISODate(iso));
export const formatDateLong = (iso) => dateLongFmt.format(parseISODate(iso));
export const formatDayMonth = (iso) => dayMonthFmt.format(parseISODate(iso));
export const formatMonthYear = (iso) => monthYearFmt.format(parseISODate(iso));
export const weekday = (iso) => weekdayFmt.format(parseISODate(iso));

export function formatStamp(isoDateTime) {
  if (!isoDateTime) return "never";
  const when = new Date(isoDateTime);
  if (Number.isNaN(when.getTime())) return String(isoDateTime);
  return `${stampFmt.format(when).replace(",", "")} IST`;
}

export function formatCaptured(isoDateTime) {
  const when = new Date(isoDateTime);
  return Number.isNaN(when.getTime()) ? String(isoDateTime) : `${stampFmt.format(when).replace(",", "")} IST`;
}

/** Current IST wall clock as "YYYY-MM-DDTHH:MM" (datetime-local value). */
export function nowIstLocal() {
  const p = Object.fromEntries(istParts.formatToParts(new Date()).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** datetime-local value -> ISO 8601 with the IST offset. */
export function localToIso(value) {
  const base = value.length === 16 ? `${value}:00` : value;
  return `${base}${IST_OFFSET}`;
}

/** Indian digit grouping on a decimal string, no float round trip: "107724.36" -> "1,07,724.36". */
export function groupIndian(text) {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(text).trim());
  if (!match) return String(text);
  const [, sign, whole, frac] = match;
  const head = whole.slice(0, -3);
  const tail = whole.slice(-3);
  const grouped = head ? `${head.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${tail}` : tail;
  return `${sign}${grouped}${frac === undefined ? "" : `.${frac}`}`;
}

export const formatINR = (text) => `₹${groupIndian(text)}`;

/** Plain grouping for foreign-currency amounts (thousands). */
export function groupWestern(text) {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(text).trim());
  if (!match) return String(text);
  const [, sign, whole, frac] = match;
  return `${sign}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${frac === undefined ? "" : `.${frac}`}`;
}

export function plural(count, one, many = `${one}s`) {
  return `${count} ${count === 1 ? one : many}`;
}

/** Display-only rounding of an API decimal string; null/blank shows an en dash. */
export function decimalPlaces(text, places) {
  if (text === null || text === undefined || text === "") return "\u2013";
  const value = Number(text);
  return Number.isFinite(value) ? value.toFixed(places) : String(text);
}
