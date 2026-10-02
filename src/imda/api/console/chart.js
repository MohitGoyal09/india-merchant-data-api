// Hand-written SVG line chart: axes, hover and keyboard tooltips, holiday ticks.
import { el, svg, clear } from "./dom.js";
import { formatDate, formatDayMonth, formatMonthYear, parseISODate } from "./format.js";

const MARGIN = { top: 14, right: 18, bottom: 30, left: 56 };
const Y_TICKS = 4;
const HOLIDAY_TICK_PX = 9;
const HOLIDAY_HIT_PX = 7;
const MIN_WIDTH = 280;
const WIDE_PX = 520;
const KEY_STEP = { ArrowLeft: -1, ArrowRight: 1, PageDown: -10, PageUp: 10 };

function niceTicks(min, max, count) {
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return { ticks, decimals: Math.max(2, -Math.floor(Math.log10(step))) };
}

function nearestIndex(xs, x) {
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= x) lo = mid;
    else hi = mid;
  }
  return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
}

/**
 * points: [{date, value (number, for geometry only), text (exact API string), source}]
 * holidays: [{date, name}] inside the range.
 * onActive({point, index, holiday}) lets the caller write the readout.
 */
export function createChart({ points, holidays, label, onActive }) {
  const node = el("div", { class: "chart-wrap" });
  const tip = el("div", { class: "tip", "aria-hidden": "true", hidden: true });
  let active = null;
  let canvas = null;
  let geometry = null;

  const times = points.map((p) => parseISODate(p.date).getTime());
  const t0 = times[0] - (points.length === 1 ? 86_400_000 : 0);
  const t1 = times[times.length - 1] + (points.length === 1 ? 86_400_000 : 0);
  const values = points.map((p) => p.value);
  const vMin = Math.min(...values);
  const vMax = Math.max(...values);
  const pad = (vMax - vMin || Math.abs(vMax) * 0.01 || 1) * 0.1;
  const domain = { min: vMin - pad, max: vMax + pad };
  const holidayTimes = holidays.map((h) => parseISODate(h.date).getTime());

  function layout() {
    const width = Math.max(MIN_WIDTH, node.clientWidth || 640);
    const height = width < WIDE_PX ? 240 : 300;
    const plot = { w: width - MARGIN.left - MARGIN.right, h: height - MARGIN.top - MARGIN.bottom };
    const x = (t) => MARGIN.left + ((t - t0) / (t1 - t0)) * plot.w;
    const y = (v) => MARGIN.top + (1 - (v - domain.min) / (domain.max - domain.min)) * plot.h;
    return { width, height, plot, x, y, xs: times.map(x) };
  }

  function drawAxes(g, geo) {
    const { ticks, decimals } = niceTicks(domain.min, domain.max, Y_TICKS);
    const right = MARGIN.left + geo.plot.w;
    const base = MARGIN.top + geo.plot.h;
    for (const v of ticks) {
      const yy = geo.y(v);
      g.append(svg("line", { class: "grid", x1: MARGIN.left, x2: right, y1: yy, y2: yy }));
      g.append(svg("text", { class: "tick-label", x: MARGIN.left - 8, y: yy + 4, "text-anchor": "end" }, v.toFixed(decimals)));
    }
    g.append(svg("line", { class: "axis", x1: MARGIN.left, x2: right, y1: base, y2: base }));
    const count = Math.max(2, Math.floor(geo.plot.w / 110));
    const longSpan = t1 - t0 > 200 * 86_400_000;
    for (let i = 0; i < count; i += 1) {
      const t = t0 + ((t1 - t0) * i) / (count - 1);
      const iso = new Date(Math.round(t / 86_400_000) * 86_400_000).toISOString().slice(0, 10);
      const anchor = i === 0 ? "start" : i === count - 1 ? "end" : "middle";
      g.append(svg("text", { class: "tick-label", x: geo.x(t), y: base + 18, "text-anchor": anchor },
        longSpan ? formatMonthYear(iso) : formatDayMonth(iso)));
    }
  }

  function drawSeries(g, geo) {
    const base = MARGIN.top + geo.plot.h;
    const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${geo.xs[i].toFixed(1)} ${geo.y(p.value).toFixed(1)}`).join(" ");
    const last = points.length - 1;
    g.append(svg("path", { class: "area", d: `${path} L${geo.xs[last].toFixed(1)} ${base} L${geo.xs[0].toFixed(1)} ${base} Z` }));
    g.append(svg("path", { class: "line", d: path }));
    for (const [i, h] of holidays.entries()) {
      const hx = geo.x(holidayTimes[i]);
      g.append(svg("line", { class: "holiday", x1: hx, x2: hx, y1: base - HOLIDAY_TICK_PX, y2: base }, svg("title", null, `${formatDate(h.date)}: ${h.name}`)));
    }
    const endX = geo.xs[last];
    g.append(svg("text", { class: "end-label", x: endX, y: geo.y(points[last].value) - 8, "text-anchor": endX > geo.width - 60 ? "end" : "start" }, points[last].text));
  }

  function draw() {
    const geo = layout();
    geometry = geo;
    const root = svg("svg", {
      class: "chart", viewBox: `0 0 ${geo.width} ${geo.height}`, role: "group", tabindex: "0",
      "aria-roledescription": "chart",
      "aria-label": `${label}. ${points.length} points from ${formatDate(points[0].date)} to ${formatDate(points[points.length - 1].date)}. Use left and right arrow keys to read values.`,
    });
    drawAxes(root, geo);
    drawSeries(root, geo);
    const cursor = svg("line", { class: "cursor", y1: MARGIN.top, y2: MARGIN.top + geo.plot.h, visibility: "hidden" });
    const dot = svg("circle", { class: "dot", r: 5, visibility: "hidden" });
    root.append(cursor, dot);
    root.addEventListener("pointermove", onPointer);
    root.addEventListener("pointerleave", onLeave);
    root.addEventListener("keydown", onKey);
    root.addEventListener("focus", onFocus);
    root.addEventListener("blur", onLeave);
    canvas = { root, cursor, dot };
    clear(node).append(root, tip);
    if (active !== null) show(active, null);
  }

  function place(index, holiday) {
    const geo = geometry;
    const xx = holiday ? geo.x(parseISODate(holiday.date).getTime()) : geo.xs[index];
    const yy = holiday ? MARGIN.top + geo.plot.h : geo.y(points[index].value);
    canvas.cursor.setAttribute("x1", xx);
    canvas.cursor.setAttribute("x2", xx);
    canvas.cursor.setAttribute("visibility", "visible");
    canvas.dot.setAttribute("visibility", holiday ? "hidden" : "visible");
    canvas.dot.setAttribute("cx", xx);
    canvas.dot.setAttribute("cy", yy);
    tip.hidden = false;
    const text = holiday ? `${formatDate(holiday.date)}: ${holiday.name}` : `${formatDate(points[index].date)}  ${points[index].text}`;
    tip.textContent = text;
    const room = geo.width - xx - 12;
    const left = tip.offsetWidth > room ? xx - tip.offsetWidth - 10 : xx + 10;
    tip.style.left = `${Math.max(4, left)}px`;
    tip.style.top = `${Math.max(2, yy - 44)}px`;
  }

  function show(index, holiday) {
    active = index;
    place(index, holiday);
    onActive?.({ point: points[index], index, holiday });
  }

  function onPointer(event) {
    const box = canvas.root.getBoundingClientRect();
    const scale = geometry.width / box.width;
    const px = (event.clientX - box.left) * scale;
    const py = (event.clientY - box.top) * scale;
    const baseline = MARGIN.top + geometry.plot.h;
    if (holidays.length && py > baseline - HOLIDAY_TICK_PX - 6) {
      const hits = holidayTimes.map((t, i) => [Math.abs(geometry.x(t) - px), i]).filter(([d]) => d <= HOLIDAY_HIT_PX);
      if (hits.length) {
        hits.sort((a, b) => a[0] - b[0]);
        show(nearestIndex(geometry.xs, px), holidays[hits[0][1]]);
        return;
      }
    }
    show(nearestIndex(geometry.xs, px), null);
  }

  function onLeave() {
    if (document.activeElement === canvas?.root) return;
    tip.hidden = true;
    canvas?.cursor.setAttribute("visibility", "hidden");
    canvas?.dot.setAttribute("visibility", "hidden");
  }

  function onFocus() {
    show(active ?? points.length - 1, null);
  }

  function onKey(event) {
    const last = points.length - 1;
    let next = null;
    if (event.key in KEY_STEP) next = (active ?? last) + KEY_STEP[event.key];
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = last;
    if (next === null) return;
    event.preventDefault();
    show(Math.min(last, Math.max(0, next)), null);
  }

  draw();
  const observer = new ResizeObserver(() => {
    if (Math.abs((node.clientWidth || 0) - (geometry?.width ?? 0)) > 1) draw();
  });
  observer.observe(node);
  return { node, destroy: () => observer.disconnect() };
}
