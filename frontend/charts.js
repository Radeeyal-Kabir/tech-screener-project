// Small dependency-free SVG charts. Text is always inserted with textContent.

const SVG = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function niceStep(range, target = 4) {
  const raw = range / target;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const candidates = [1, 2, 2.5, 5, 10].map((m) => m * pow);
  return candidates.find((c) => range / c <= target + 1) ?? candidates.at(-1);
}

export const pct = (v, digits = 1, signed = false) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const s = (v * 100).toFixed(digits);
  return `${signed && v > 0 ? "+" : ""}${s.replace("-", "−")}%`;
};

const shortDate = (iso) => {
  const d = new Date(`${iso}T00:00:00Z`);
  return d.toLocaleDateString("en-US", { month: "short", year: "2-digit", timeZone: "UTC" }).replace(" ", " '");
};

/**
 * Multi-series line chart over quarters with a crosshair tooltip.
 * rows: [{end, ...}], series: [{key, label, color}] where color is a CSS var.
 */
export function lineChart(root, rows, series, { height = 190, format = (v) => pct(v) } = {}) {
  root.classList.add("chart");
  root.tabIndex = 0;
  root.setAttribute("role", "img");
  root.setAttribute(
    "aria-label",
    `${series.map((s) => s.label).join(" and ")} by quarter. Use left and right arrow keys to read values.`,
  );

  let active = null;
  let geom = null;
  const tooltip = document.createElement("div");
  tooltip.className = "tooltip";
  tooltip.hidden = true;

  function draw() {
    const width = root.clientWidth;
    if (!width) return;
    root.replaceChildren();
    const m = { top: 10, right: 58, bottom: 22, left: 40 };
    const w = width - m.left - m.right;
    const h = height - m.top - m.bottom;

    const values = rows.flatMap((r) => series.map((s) => r[s.key])).filter((v) => v !== null && v !== undefined);
    if (!values.length) return;
    let lo = Math.min(0, ...values);
    let hi = Math.max(0, ...values);
    const step = niceStep(hi - lo || 0.1);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    const x = (i) => m.left + (rows.length === 1 ? w / 2 : (i * w) / (rows.length - 1));
    const y = (v) => m.top + h - ((v - lo) / (hi - lo)) * h;

    const svg = svgEl("svg", { width, height, "aria-hidden": "true" });
    for (let t = lo; t <= hi + step / 2; t += step) {
      const yy = y(t);
      svg.append(svgEl("line", { x1: m.left, x2: m.left + w, y1: yy, y2: yy,
        class: Math.abs(t) < step / 1000 ? "baseline" : "gridline" }));
      const label = svgEl("text", { x: m.left - 6, y: yy + 4, "text-anchor": "end" });
      label.textContent = pct(t, step < 0.01 ? 1 : 0);
      svg.append(label);
    }
    const every = Math.ceil(rows.length / Math.max(1, Math.floor(w / 64)));
    const last = rows.length - 1;
    rows.forEach((r, i) => {
      const isTick = i % every === 0 || i === last;
      // The last quarter is always labeled; drop a regular tick that would collide with it.
      if (!isTick || (i !== last && x(last) - x(i) < 56)) return;
      const label = svgEl("text", { x: x(i), y: height - 4, "text-anchor": "middle" });
      label.textContent = shortDate(r.end);
      svg.append(label);
    });

    const crosshair = svgEl("line", { class: "crosshair", y1: m.top, y2: m.top + h, visibility: "hidden" });
    svg.append(crosshair);

    const ends = [];
    for (const s of series) {
      let d = "";
      let penUp = true;
      rows.forEach((r, i) => {
        const v = r[s.key];
        if (v === null || v === undefined) { penUp = true; return; }
        d += `${penUp ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        penUp = false;
      });
      svg.append(svgEl("path", { d, fill: "none", stroke: `var(${s.color})`, "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round" }));
      const lastIdx = rows.map((r) => r[s.key]).findLastIndex((v) => v !== null && v !== undefined);
      if (lastIdx >= 0) {
        const v = rows[lastIdx][s.key];
        svg.append(svgEl("circle", { cx: x(lastIdx), cy: y(v), r: 4, fill: `var(${s.color})`,
          stroke: "var(--surface)", "stroke-width": 2 }));
        ends.push({ x: x(lastIdx), y: y(v), text: format(v) });
      }
    }
    // Direct end labels only when they don't collide; legend + tooltip carry the rest.
    const collide = ends.some((a, i) => ends.some((b, j) => i < j && Math.abs(a.y - b.y) < 13));
    if (!collide) {
      for (const e of ends) {
        const label = svgEl("text", { x: e.x + 8, y: e.y + 4, class: "end-label" });
        label.textContent = e.text;
        svg.append(label);
      }
    }

    root.append(svg, tooltip);
    geom = { x, m, w, h, crosshair };
    if (active !== null) show(active);
  }

  function show(i) {
    active = Math.max(0, Math.min(rows.length - 1, i));
    const { x, m, crosshair } = geom;
    const xx = x(active);
    crosshair.setAttribute("x1", xx);
    crosshair.setAttribute("x2", xx);
    crosshair.setAttribute("visibility", "visible");

    tooltip.replaceChildren();
    const head = document.createElement("div");
    head.className = "t-head";
    head.textContent = `Quarter ending ${rows[active].end}`;
    tooltip.append(head);
    for (const s of series) {
      const row = document.createElement("div");
      row.className = "t-row";
      const key = document.createElement("i");
      key.style.setProperty("--key", `var(${s.color})`);
      const val = document.createElement("strong");
      val.textContent = format(rows[active][s.key]);
      const name = document.createElement("span");
      name.textContent = s.label;
      row.append(key, val, name);
      tooltip.append(row);
    }
    tooltip.hidden = false;
    const tw = tooltip.offsetWidth;
    const left = xx + 12 + tw > root.clientWidth ? xx - 12 - tw : xx + 12;
    tooltip.style.left = `${Math.max(0, left)}px`;
    tooltip.style.top = `${m.top}px`;
  }

  function hide() {
    active = null;
    tooltip.hidden = true;
    geom?.crosshair.setAttribute("visibility", "hidden");
  }

  root.addEventListener("pointermove", (e) => {
    if (!geom) return;
    const rect = root.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const { m, w } = geom;
    const i = rows.length === 1 ? 0 : Math.round(((px - m.left) / w) * (rows.length - 1));
    show(i);
  });
  root.addEventListener("pointerleave", hide);
  root.addEventListener("focus", () => geom && show(rows.length - 1));
  root.addEventListener("blur", hide);
  root.addEventListener("keydown", (e) => {
    if (!geom) return;
    if (e.key === "ArrowLeft") { show((active ?? rows.length) - 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { show((active ?? -1) + 1); e.preventDefault(); }
    if (e.key === "Escape") hide();
  });

  new ResizeObserver(draw).observe(root);
}

/** Single-series price sparkline with an end dot. */
export function sparkline(root, closes, { height = 36 } = {}) {
  root.classList.add("sparkline");
  function draw() {
    const width = root.clientWidth;
    if (!width || closes.length < 2) return;
    const vals = closes.map((c) => c[1]);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = 5;
    const x = (i) => pad + (i * (width - 2 * pad)) / (vals.length - 1);
    const y = (v) => pad + (height - 2 * pad) * (1 - (hi === lo ? 0.5 : (v - lo) / (hi - lo)));
    const svg = svgEl("svg", { width, height, "aria-hidden": "true" });
    svg.append(svgEl("path", {
      d: vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""),
      fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    svg.append(svgEl("circle", { cx: x(vals.length - 1), cy: y(vals.at(-1)), r: 4, fill: "var(--series-1)",
      stroke: "var(--surface)", "stroke-width": 2 }));
    root.replaceChildren(svg);
  }
  new ResizeObserver(draw).observe(root);
}
