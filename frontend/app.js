import { lineChart, pct, sparkline } from "./charts.js";

const REPO_URL = "https://github.com/radeeyal-kabir/tech-screener-project";
const SCORE_URL = `${REPO_URL}/blob/main/screener/score.py`;
const RATING_RANK = { Avoid: 0, Hold: 1, Buy: 2 };
const RECENT_CHANGE_DAYS = 14;

const FLAG_NAMES = {
  demand_weakness: "Demand weakness",
  margin_pressure: "Margin pressure",
  pricing_pressure: "Pricing pressure",
  supply_chain: "Supply chain",
  inventory_buildup: "Inventory build-up",
  customer_concentration: "Customer concentration",
  competition: "Competition",
  export_controls_geopolitical: "Export controls / geopolitics",
  regulatory_legal: "Regulatory / legal",
  liquidity_debt: "Liquidity / debt",
  restructuring_layoffs: "Restructuring / layoffs",
  impairment_writedown: "Impairment / write-down",
  accounting_controls: "Accounting / controls",
  guidance_cut: "Guidance cut",
  macro_fx: "Macro / FX",
  other: "Other",
};

// ---------------------------------------------------------------- helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const secUrl = (u) => (typeof u === "string" && u.startsWith("https://www.sec.gov/") ? u : null);
const dir = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
const num = (v, d = 2) => (v === null || v === undefined ? "–" : v.toFixed(d));
const pts = (v) => (v === null || v === undefined ? null : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1).replace("-", "−")} pts`);
const billions = (v) => (v === null || v === undefined ? "–" : `$${(v / 1e9).toFixed(1)}B`);
const flagName = (c) => FLAG_NAMES[c] ?? c.replaceAll("_", " ");
const ordinal = (n) => `${n}${["th", "st", "nd", "rd"][(n % 100 > 10 && n % 100 < 14) || n % 10 > 3 ? 0 : n % 10]}`;
const fmtTime = (iso) =>
  iso ? new Date(iso).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" }) : "never";
const fmtDay = (iso) =>
  new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const daysAgo = (iso) => (Date.now() - new Date(iso).getTime()) / 86_400_000;

function ratingBadge(score) {
  if (!score) return el("span", { class: "rating", text: "Not rated" });
  return el("span", { class: `rating ${score.rating}` }, score.rating, " ", el("span", { class: "score", text: score.composite.toFixed(0) }));
}

function change(v, extra = "") {
  return el("span", { class: `chg ${dir(v)}`, text: `${pct(v, 1, true)}${extra}` });
}

async function loadJson(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

// ---------------------------------------------------------------- sections

function renderAsOf(companiesDoc, prices) {
  const companies = companiesDoc.companies ?? [];
  const newest = companies
    .flatMap((c) => (c.qualitative?.filings ?? []).filter((f) => f.status === "ok").map((f) => ({ ...f, ticker: c.ticker })))
    .sort((a, b) => b.filed.localeCompare(a.filed))[0];
  const rows = [
    ["Prices", prices?.market_date ? `${prices.market_date} close · refreshed ${fmtTime(prices.as_of)}` : "not loaded yet"],
    ["Scores & fundamentals", fmtTime(companiesDoc.as_of)],
    ["Newest filing analyzed", newest ? `${newest.ticker} ${newest.form}, filed ${newest.filed}` : "none yet"],
  ];
  document.getElementById("asof").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v }))));
}

function renderMovers(ctx, period) {
  const movers = ctx.prices.movers[period];
  const key = period === "day" ? "change_1d" : "change_5d";
  const list = (tickers) =>
    el("ol", {}, tickers.map((t) => {
      const p = ctx.priceBy[t];
      const c = ctx.companyBy[t];
      return el("li", {},
        el("span", { class: "tk", text: t }),
        el("span", {}, ratingBadge(c?.score)),
        el("span", { class: `chg ${dir(p[key])}`, text: pct(p[key], 1, true) }));
    }));
  document.getElementById("movers").replaceChildren(
    el("div", {}, el("h3", { text: "Biggest gains" }), list(movers.winners)),
    el("div", {}, el("h3", { text: "Biggest losses" }), list(movers.losers)),
  );
}

/** Where price action and the rating point in opposite directions. */
export function findTensions(companies, priceBy) {
  const out = [];
  for (const c of companies) {
    const p = priceBy[c.ticker];
    if (!c.score || !p) continue;
    const { rating } = c.score;
    const d1 = p.change_1d ?? 0;
    const d5 = p.change_5d ?? 0;
    const move = Math.abs(d5) >= Math.abs(d1) ? { v: d5, when: "this week" } : { v: d1, when: "today" };
    const hist = c.rating_history ?? [];
    const last = hist.length > 1 ? hist.at(-1) : null;
    const recent = last && daysAgo(last.at) <= RECENT_CHANGE_DAYS ? last : null;
    const prevRank = recent ? RATING_RANK[hist.at(-2).rating] : null;
    const push = (weight, text) => out.push({ ticker: c.ticker, weight, text });

    if (recent && RATING_RANK[recent.rating] < prevRank && d5 >= 0.03) {
      push(d5 + 1, `up ${pct(d5)} this week despite a downgrade to ${recent.rating} on ${fmtDay(recent.at)}`);
    } else if (recent && RATING_RANK[recent.rating] > prevRank && d5 <= -0.03) {
      push(-d5 + 1, `down ${pct(-d5)} this week despite an upgrade to ${recent.rating} on ${fmtDay(recent.at)}`);
    } else if (rating === "Avoid" && (d1 >= 0.02 || d5 >= 0.05)) {
      push(move.v, `up ${pct(move.v)} ${move.when} despite an Avoid rating`);
    } else if (rating === "Buy" && (d1 <= -0.02 || d5 <= -0.05)) {
      push(-move.v, `down ${pct(-move.v)} ${move.when} despite a Buy rating`);
    } else if (rating === "Avoid" && (p.vs_sma200 ?? 0) >= 0.10 && (p.rel_universe_3m ?? 0) >= 0.05) {
      push(p.vs_sma200 / 2, `trading ${pct(p.vs_sma200, 0)} above its 200-day average and outperforming peers, despite an Avoid rating`);
    } else if (rating === "Buy" && (p.vs_sma200 ?? 0) <= -0.10 && (p.rel_universe_3m ?? 0) <= -0.05) {
      push(-p.vs_sma200 / 2, `trading ${pct(-p.vs_sma200, 0)} below its 200-day average and lagging peers, despite a Buy rating`);
    }
  }
  return out.sort((a, b) => b.weight - a.weight);
}

function renderTension(ctx) {
  const items = findTensions(ctx.companies, ctx.priceBy);
  const ul = document.getElementById("tension");
  if (!items.length) {
    ul.replaceChildren(el("li", { class: "empty", text: "No price-vs-rating disagreements right now." }));
    return;
  }
  ul.replaceChildren(...items.map((t) =>
    el("li", {}, el("span", { class: "tk", text: t.ticker }), el("span", { text: `${t.text}.` }), ratingBadge(ctx.companyBy[t.ticker].score))));
}

const COLUMNS = [
  { label: "Company", left: true, sort: (r) => r.c.ticker,
    cell: (r) => [el("a", { href: `#card-${r.c.ticker}`, text: r.c.ticker }), " ", el("span", { class: "sub", text: r.c.name })] },
  { label: "Price", sort: (r) => r.p?.close, cell: (r) => (r.p ? `$${r.p.close.toFixed(2)}` : "–") },
  { label: "1D", sort: (r) => r.p?.change_1d, cell: (r) => change(r.p?.change_1d) },
  { label: "5D", sort: (r) => r.p?.change_5d, cell: (r) => change(r.p?.change_5d) },
  { label: "Rating", left: true, sort: (r) => r.c.score?.composite, cell: (r) => ratingBadge(r.c.score) },
  { label: "Rev YoY", sort: (r) => r.f?.revenue_yoy, cell: (r) => pct(r.f?.revenue_yoy, 0, true) },
  { label: "Net margin", sort: (r) => r.f?.net_margin, cell: (r) => pct(r.f?.net_margin, 0) },
  { label: "Debt/equity", sort: (r) => r.f?.debt_to_equity, cell: (r) => num(r.f?.debt_to_equity) },
  { label: "vs 200-day", sort: (r) => r.p?.vs_sma200, cell: (r) => change(r.p?.vs_sma200) },
  { label: "3M vs peers", sort: (r) => r.p?.rel_universe_3m, cell: (r) => pts(r.p?.rel_universe_3m) ?? "–" },
];

function renderTable(ctx) {
  const table = document.getElementById("grid");
  const rows = ctx.companies.map((c) => ({ c, p: ctx.priceBy[c.ticker], f: c.fundamentals?.latest }));
  let sortIdx = 2;
  let sortDir = -1;

  function paint() {
    const col = COLUMNS[sortIdx];
    const sorted = [...rows].sort((a, b) => {
      const va = col.sort(a);
      const vb = col.sort(b);
      if (va === undefined || va === null) return 1;
      if (vb === undefined || vb === null) return -1;
      return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sortDir;
    });
    const head = el("tr", {}, COLUMNS.map((c, i) => {
      const th = el("th", {
        class: c.left ? "left" : null, scope: "col", tabindex: "0",
        "aria-sort": i === sortIdx ? (sortDir > 0 ? "ascending" : "descending") : "none",
        text: c.label,
      });
      const activate = () => {
        if (sortIdx === i) sortDir = -sortDir;
        else { sortIdx = i; sortDir = i === 0 ? 1 : -1; }
        paint();
        table.querySelectorAll("th")[i].focus();
      };
      th.addEventListener("click", activate);
      th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } });
      return th;
    }));
    const body = sorted.map((r) => el("tr", {}, COLUMNS.map((c) => el("td", { class: c.left ? "left" : null }, c.cell(r)))));
    table.replaceChildren(el("thead", {}, head), el("tbody", {}, body));
  }
  paint();
}

function toneSummary(qual) {
  if (!qual) return "No filing analyzed yet";
  if (qual.consecutive_bearish >= 2) return `Bearish, ${ordinal(qual.consecutive_bearish)} consecutive filing`;
  const shift = qual.tone_shift > 0 ? ", improving" : qual.tone_shift < 0 ? ", worsening" : "";
  return `${qual.tone[0].toUpperCase()}${qual.tone.slice(1)}${shift}`;
}

function card(c, p) {
  const f = c.fundamentals;
  const s = c.score;
  const filings = c.qualitative?.filings ?? [];
  const latestOk = [...filings].reverse().find((x) => x.status === "ok");
  const persist = Object.fromEntries((s?.qualitative?.flags ?? []).map((x) => [x.category, x.consecutive_filings]));

  const metric = (label, value, trend) => el("div", {}, el("dt", { text: label }), el("dd", {}, value, trend ? el("span", { class: "trend", text: trend }) : null));
  const slope = f?.trend.revenue_yoy_slope;
  const growthTrend = slope === null || slope === undefined ? null : slope > 0.005 ? "accelerating" : slope < -0.005 ? "slowing" : "steady";

  const spark = el("div");
  if (p?.closes) sparkline(spark, p.closes);

  const chartBox = el("div");
  const more = el("details", { class: "more" }, el("summary", { text: "Trends, filings & score breakdown" }));
  if (f?.quarters?.length) {
    more.append(
      el("h4", { text: "Revenue growth and net margin, last 8 quarters" }),
      el("div", { class: "legend" },
        el("span", { style: "--key: var(--series-1)" }, el("i"), "Revenue growth YoY"),
        el("span", { style: "--key: var(--series-2)" }, el("i"), "Net margin")),
      chartBox,
      el("div", { class: "mini-scroll" }, el("table", { class: "mini" },
        el("thead", {}, el("tr", {}, ["Quarter end", "Revenue", "Rev YoY", "Net margin", "D/E", "Current ratio"].map((h) => el("th", { text: h })))),
        el("tbody", {}, [...f.quarters].reverse().map((q) => el("tr", {},
          el("td", { text: q.end }), el("td", { text: billions(q.revenue) }), el("td", { text: pct(q.revenue_yoy, 1, true) }),
          el("td", { text: pct(q.net_margin, 1) }), el("td", { text: num(q.debt_to_equity) }), el("td", { text: num(q.current_ratio) })))))),
    );
    lineChart(chartBox, f.quarters, [
      { key: "revenue_yoy", label: "Revenue growth YoY", color: "--series-1" },
      { key: "net_margin", label: "Net margin", color: "--series-2" },
    ]);
  }

  if (s) {
    const qp = s.quant.parts;
    const quantBits = [`growth ${qp.revenue_growth.toFixed(0)}`, `margin ${qp.net_margin.toFixed(0)}`];
    if ("leverage" in qp) quantBits.push(`leverage ${qp.leverage.toFixed(0)}`);
    quantBits.push(`liquidity ${qp.liquidity.toFixed(0)}`);
    const lines = [`Quant ${s.quant.score.toFixed(0)} × 70%: ${quantBits.join(", ")}`];
    if (s.quant.negative_equity) {
      lines.push("Debt/equity excluded (negative equity, so the ratio is undefined); the other three quant inputs are reweighted to fill its share.");
    }
    if (s.qualitative) {
      lines.push(`Qualitative ${s.qualitative.score.toFixed(0)} × 30%: tone ${s.qualitative.parts.tone.toFixed(0)}, red flags ${s.qualitative.parts.red_flags.toFixed(0)}`);
    } else {
      lines.push("Qualitative pending: composite uses the quant score alone");
    }
    more.append(el("h4", { text: "Score breakdown (each part 0–100)" }), el("ul", {}, lines.map((t) => el("li", { text: t }))),
      el("p", { class: "small" }, "Anchor points and weights: ", el("a", { href: SCORE_URL, text: "screener/score.py" })));
  }

  if (filings.length) {
    more.append(el("h4", { text: "Filings analyzed (MD&A)" }), el("ul", {}, [...filings].reverse().map((x) => {
      const src = secUrl(x.source_url);
      const desc = x.status === "ok"
        ? `${x.tone}, ${x.red_flags.length} red flag${x.red_flags.length === 1 ? "" : "s"}${x.tone_rationale ? ` · ${x.tone_rationale}` : ""}`
        : `analysis unavailable (${x.status.replace("_", " ")})`;
      return el("li", {}, `${x.form} for period ending ${x.period_end ?? "?"} (filed ${x.filed}): ${desc} `, src ? el("a", { href: src, text: "source" }) : null);
    })));
  }

  if (c.recent_filings?.length) {
    more.append(el("h4", { text: "Recent SEC filings" }), el("ul", {}, c.recent_filings.map((e) => {
      const u = secUrl(e.url);
      const label = `${e.form} ${e.filed}${e.items?.length ? `: ${e.items.join(", ")}` : ""}`;
      return el("li", {}, u ? el("a", { href: u, text: label }) : label);
    })));
  }

  const notes = [...(f?.warnings ?? []), ...(s?.quant.missing ?? []).map((m) => `Missing input scored neutral: ${m.replaceAll("_", " ")}`)];
  more.append(
    el("h4", { text: "Data freshness" }),
    el("ul", {},
      el("li", { text: `Fundamentals: ${fmtTime(c.fundamentals_as_of)} (latest quarter ${f?.latest.period_end ?? "–"})` }),
      el("li", { text: `Qualitative: ${fmtTime(c.qualitative_as_of)}` }),
      el("li", { text: `Price: ${p ? `${p.date} close (${p.source})` : "–"}` }),
      notes.map((n) => el("li", { class: "warn", text: n }))),
  );

  const flags = latestOk?.red_flags ?? [];
  return el("article", { class: "card", id: `card-${c.ticker}`, "data-rating": s?.rating ?? "" },
    el("div", { class: "card-head" },
      el("div", { class: "who" }, el("h3", { text: `${c.ticker} · ${c.name}` }), el("div", { class: "sector", text: c.sub_sector })),
      el("div", { class: "px" },
        el("div", { class: "price", text: p ? `$${p.close.toFixed(2)}` : "–" }),
        p ? change(p.change_1d, " today") : null)),
    spark,
    el("div", { class: "rating-row" }, ratingBadge(s), el("a", { href: SCORE_URL, class: "small", text: "How this is scored" })),
    s ? el("p", { class: "rationale", text: s.rationale }) : el("p", { class: "rationale", text: "No fundamentals yet." }),
    f ? el("dl", { class: "metrics" },
      metric("Revenue YoY", pct(f.latest.revenue_yoy, 1, true), growthTrend),
      metric("Net margin", pct(f.latest.net_margin, 1), pts(f.trend.net_margin_change_yoy) && `${pts(f.trend.net_margin_change_yoy)} YoY`),
      metric("Debt/equity",
        s?.quant?.negative_equity ? "N/A (negative equity)" : num(f.latest.debt_to_equity),
        s?.quant?.negative_equity ? "excluded from quant score"
          : (f.trend.debt_to_equity_change_yoy === null ? null : `${f.trend.debt_to_equity_change_yoy > 0 ? "+" : ""}${num(f.trend.debt_to_equity_change_yoy)} YoY`)),
      metric("Current ratio", num(f.latest.current_ratio))) : null,
    p ? el("div", { class: "momentum" },
      el("strong", { text: "Momentum " }), el("span", { class: "note", text: "(shown separately, not in the score): " }),
      `vs 50-day ${pct(p.vs_sma50, 1, true)} · vs 200-day ${pct(p.vs_sma200, 1, true)} · 3M vs peers ${pts(p.rel_universe_3m) ?? "–"}`) : null,
    el("div", { class: "tone-row" },
      el("strong", { text: "Tone" }),
      el("span", { class: "tone-strip", role: "img", "aria-label": `Tone by filing, oldest to newest: ${filings.map((x) => x.tone ?? "unavailable").join(", ") || "none"}` },
        filings.map((x) => el("span", { class: `tone-dot ${x.status === "ok" ? x.tone : "failed"}`, title: `${x.form} ${x.period_end ?? ""}: ${x.tone ?? "unavailable"}` }))),
      el("span", { text: toneSummary(s?.qualitative) })),
    flags.length ? el("ul", { class: "flags" }, flags.map((rf) => el("li", {},
      el("span", { class: "cat", text: flagName(rf.category) }),
      persist[rf.category] >= 2 ? el("span", { class: "persist", text: ` · ${persist[rf.category]} filings running` }) : null,
      el("div", { text: rf.summary }),
      rf.quote ? el("blockquote", { text: `“${rf.quote}”` }) : null))) : null,
    more,
  );
}

function renderCards(ctx, rating) {
  const sorted = [...ctx.companies].sort((a, b) => (b.score?.composite ?? -1) - (a.score?.composite ?? -1));
  const shown = sorted.filter((c) => !rating || c.score?.rating === rating);
  document.getElementById("cards").replaceChildren(...shown.map((c) => card(c, ctx.priceBy[c.ticker])));
}

function wireSegmented(container, attr, onChange) {
  container.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    for (const b of container.querySelectorAll("button")) b.setAttribute("aria-checked", String(b === btn));
    onChange(btn.dataset[attr]);
  });
}

// ---------------------------------------------------------------- main

async function main() {
  document.getElementById("methodology-link").href = SCORE_URL;
  document.getElementById("repo-link").href = REPO_URL;
  const status = document.getElementById("status");

  let companiesDoc;
  let prices;
  try {
    [companiesDoc, prices] = await Promise.all([loadJson("data/companies.json"), loadJson("data/prices.json")]);
  } catch (err) {
    status.textContent = `Couldn't load data (${err.message}).`;
    status.classList.add("error");
    return;
  }

  const companies = companiesDoc.companies ?? [];
  const ctx = {
    companies,
    prices,
    companyBy: Object.fromEntries(companies.map((c) => [c.ticker, c])),
    priceBy: Object.fromEntries((prices.prices ?? []).map((p) => [p.ticker, p])),
  };
  renderAsOf(companiesDoc, prices);

  const hasPrices = (prices.prices ?? []).length > 0;
  const hasScores = companies.some((c) => c.score);
  if (!hasPrices && !hasScores) {
    status.textContent = "No data yet. Run the Backfill workflow (Actions → Backfill → Run workflow) to seed the dataset.";
    return;
  }
  status.hidden = true;

  if (hasPrices) {
    document.getElementById("movers-section").hidden = false;
    renderMovers(ctx, "day");
    wireSegmented(document.querySelector("#movers-section .segmented"), "period", (p) => renderMovers(ctx, p));
  }
  if (hasPrices && hasScores) {
    document.getElementById("tension-section").hidden = false;
    renderTension(ctx);
  }
  if (companies.length) {
    document.getElementById("table-section").hidden = false;
    renderTable(ctx);
    document.getElementById("cards-section").hidden = false;
    renderCards(ctx, "");
    wireSegmented(document.getElementById("rating-filter"), "rating", (r) => renderCards(ctx, r));
  }
}

main();
