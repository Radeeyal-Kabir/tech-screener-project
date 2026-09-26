"""Fundamentals pipeline: SEC EDGAR companyfacts (XBRL) -> quarterly quant ratios.

Usage:
    python -m screener.fetch_fundamentals              # all tracked companies
    python -m screener.fetch_fundamentals NVDA AAPL    # just these

Filers don't tag the same line item the same way: hardware and software
companies (and the same company before/after ASC 606) use different XBRL
concept names. Each metric therefore has an ordered list of candidate
concepts in CONCEPTS; see ``_pick_series`` for how they're merged.

Companies only report Q4 inside the annual 10-K, so Q4 for flow metrics
(revenue, net income) is derived as annual minus Q1-Q3.

All-or-nothing: if any company fails (EDGAR unreachable, required concept
missing), nothing is written and the process exits non-zero.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from screener import edgar_client, store
from screener.universe import tickers as all_tickers

CONCEPTS: dict[str, list[str]] = {
    # Flow (duration) concepts.
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    # Point-in-time (instant) concepts.
    "assets_current": ["AssetsCurrent"],
    "liabilities_current": ["LiabilitiesCurrent"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    # Debt: a "total" concept (includes current maturities) is preferred;
    # otherwise noncurrent + current portion are summed.
    "debt_total": [
        "LongTermDebt",
        "DebtLongtermAndShorttermCombinedAmount",
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    ],
    "debt_noncurrent": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "ConvertibleDebtNoncurrent",  # e.g. PANW: switched off LongTermDebt entirely for convertible notes
    ],
    "debt_current": [
        "LongTermDebtCurrent",
        "DebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
        "ConvertibleDebtCurrent",
    ],
}

REQUIRED = ("revenue", "net_income", "assets_current", "liabilities_current", "equity")

QUARTERS_KEPT = 8
_QUARTER_DAYS = (80, 100)  # 13- or 14-week quarters on 52/53-week fiscal calendars
_ANNUAL_DAYS = (350, 380)
_STALE_AFTER_DAYS = 200


class FundamentalsError(RuntimeError):
    pass


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _usd_facts(companyfacts: dict, concept: str) -> list[dict]:
    node = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    return [
        f
        for f in node.get("units", {}).get("USD", [])
        if f.get("form", "").startswith(("10-Q", "10-K"))
    ]


def _dedupe_latest_filed(facts: list[dict], key) -> dict:
    """Same period can appear in several filings (comparatives, restatements):
    keep the most recently filed value."""
    out: dict = {}
    for f in facts:
        k = key(f)
        if k not in out or f["filed"] > out[k]["filed"]:
            out[k] = f
    return out


def instant_series(facts: list[dict]) -> dict[date, float]:
    deduped = _dedupe_latest_filed(facts, key=lambda f: f["end"])
    return {_d(end): float(f["val"]) for end, f in deduped.items()}


def quarterly_flow_series(facts: list[dict]) -> dict[date, float]:
    """Return {quarter_end: 3-month value}, deriving Q4 from the annual figure
    where the company only reported Q4 inside its 10-K."""
    deduped = _dedupe_latest_filed(
        [f for f in facts if "start" in f], key=lambda f: (f["start"], f["end"])
    )
    quarters: dict[date, tuple[date, float]] = {}
    quarter_filed: dict[date, str] = {}
    annuals: list[tuple[date, date, float]] = []
    for (start_s, end_s), f in deduped.items():
        start, end = _d(start_s), _d(end_s)
        days = (end - start).days
        if _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]:
            if end not in quarters or f["filed"] > quarter_filed[end]:
                quarters[end] = (start, float(f["val"]))
                quarter_filed[end] = f["filed"]
        elif _ANNUAL_DAYS[0] <= days <= _ANNUAL_DAYS[1]:
            annuals.append((start, end, float(f["val"])))

    for fy_start, fy_end, fy_val in annuals:
        if fy_end in quarters:
            continue
        inner = [
            (q_end, q_start, v)
            for q_end, (q_start, v) in quarters.items()
            if q_start >= fy_start - timedelta(days=7) and q_end <= fy_end - timedelta(days=60)
        ]
        if len(inner) != 3:
            continue  # can't derive Q4 cleanly; leave the gap rather than guess
        q3_end = max(e for e, _, _ in inner)
        quarters[fy_end] = (q3_end + timedelta(days=1), fy_val - sum(v for _, _, v in inner))

    return {end: v for end, (_, v) in quarters.items()}


def _pick_series(companyfacts: dict, metric: str, *, flow: bool) -> tuple[dict[date, float], str | None]:
    """Merge candidate concepts for one metric. The concept with the most
    recent data is primary (handles companies that switched tags); older
    periods it lacks are filled from the other candidates in priority order."""
    build = quarterly_flow_series if flow else instant_series
    candidates = []
    for priority, concept in enumerate(CONCEPTS[metric]):
        series = build(_usd_facts(companyfacts, concept))
        if series:
            candidates.append((max(series), -priority, concept, series))
    if not candidates:
        return {}, None
    candidates.sort(reverse=True)
    primary_concept = candidates[0][2]
    merged: dict[date, float] = {}
    for _, _, _, series in candidates:
        for d, v in series.items():
            merged.setdefault(d, v)
    return merged, primary_concept


def _at(series: dict[date, float], target: date, tolerance_days: int = 7) -> float | None:
    if target in series:
        return series[target]
    best = min(series, key=lambda d: abs((d - target).days), default=None)
    if best is not None and abs((best - target).days) <= tolerance_days:
        return series[best]
    return None


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _slope(values: list[float | None]) -> float | None:
    """Least-squares slope per quarter; None if fewer than 3 points."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(i for i, _ in pts) / n
    my = sum(v for _, v in pts) / n
    den = sum((i - mx) ** 2 for i, _ in pts)
    return sum((i - mx) * (v - my) for i, v in pts) / den if den else None


def _round(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def compute_fundamentals(companyfacts: dict, *, today: date | None = None) -> dict:
    today = today or date.today()
    series: dict[str, dict[date, float]] = {}
    concepts_used: dict[str, str | None] = {}
    for metric in CONCEPTS:
        flow = metric in ("revenue", "net_income")
        series[metric], concepts_used[metric] = _pick_series(companyfacts, metric, flow=flow)

    missing = [m for m in REQUIRED if not series[m]]
    if missing:
        raise FundamentalsError(
            f"No XBRL data for {missing} under any candidate concept "
            f"({ {m: CONCEPTS[m] for m in missing} }). Add the filer's concept name to CONCEPTS."
        )

    warnings: list[str] = []
    rev = series["revenue"]
    ends = sorted(rev)
    # Keep 4 extra quarters so the oldest kept quarter still has a YoY comparison.
    window = ends[-(QUARTERS_KEPT + 4):]

    def debt_at(d: date) -> float | None:
        total = _at(series["debt_total"], d)
        if total is not None:
            return total
        noncurrent = _at(series["debt_noncurrent"], d)
        if noncurrent is None:
            return None
        return noncurrent + (_at(series["debt_current"], d) or 0.0)

    rows = []
    for end in window:
        revenue = rev[end]
        net_income = series["net_income"].get(end)
        equity = _at(series["equity"], end)
        debt = debt_at(end)
        prior_rev = _at(rev, end - timedelta(days=364), tolerance_days=20)
        rows.append(
            {
                "end": end.isoformat(),
                "revenue": revenue,
                "net_income": net_income,
                "net_margin": _round(_ratio(net_income, revenue)),
                "revenue_yoy": _round(_ratio(revenue, prior_rev) - 1 if prior_rev else None),
                # D/E is meaningless with negative equity (buyback-heavy filers).
                "debt_to_equity": _round(_ratio(debt, equity)) if equity and equity > 0 else None,
                "current_ratio": _round(
                    _ratio(_at(series["assets_current"], end), _at(series["liabilities_current"], end))
                ),
                "negative_equity": equity is not None and equity <= 0,
            }
        )
    rows = rows[-QUARTERS_KEPT:]
    latest = rows[-1]

    if (today - _d(latest["end"])).days > _STALE_AFTER_DAYS:
        warnings.append(
            f"Latest quarter ({latest['end']}) is over {_STALE_AFTER_DAYS} days old — "
            "the filer may have switched to a concept not in CONCEPTS."
        )
    if latest["negative_equity"]:
        warnings.append("Negative stockholders' equity: debt/equity not meaningful.")
    if not series["debt_total"] and not series["debt_noncurrent"]:
        warnings.append("No debt concept found; debt/equity unavailable.")

    def col(name: str) -> list[float | None]:
        return [r[name] for r in rows]

    def change_yoy(name: str) -> float | None:
        vals = col(name)
        if len(vals) < 5 or vals[-1] is None or vals[-5] is None:
            return None
        return _round(vals[-1] - vals[-5])

    return {
        "quarters": rows,
        "latest": {
            "period_end": latest["end"],
            "revenue_yoy": latest["revenue_yoy"],
            "net_margin": latest["net_margin"],
            "debt_to_equity": latest["debt_to_equity"],
            "current_ratio": latest["current_ratio"],
        },
        "trend": {
            # Direction of YoY growth across the last 4 quarters, per quarter.
            "revenue_yoy_slope": _round(_slope(col("revenue_yoy")[-4:])),
            # Same-quarter-last-year comparisons avoid seasonality.
            "net_margin_change_yoy": change_yoy("net_margin"),
            "debt_to_equity_change_yoy": change_yoy("debt_to_equity"),
        },
        "concepts_used": concepts_used,
        "warnings": warnings,
    }


def update_fundamentals(state: dict, tickers: list[str]) -> None:
    """Fetch + compute for ``tickers`` into ``state`` (in memory). Raises on
    any failure so the caller never persists a partial update."""
    ciks = edgar_client.resolve_ciks(tickers)
    for t in tickers:
        facts = edgar_client.get_companyfacts(ciks[t])
        try:
            fundamentals = compute_fundamentals(facts)
        except FundamentalsError as exc:
            raise FundamentalsError(f"{t}: {exc}") from exc
        rec = state["companies"][t]
        rec["cik"] = ciks[t]
        rec["fundamentals"] = fundamentals
        rec["fundamentals_as_of"] = store.utc_now_iso()
        print(f"{t}: latest quarter {fundamentals['latest']['period_end']}", file=sys.stderr)


def main(argv: list[str]) -> int:
    from screener import score

    tickers = [t.upper() for t in argv] or all_tickers()
    state = store.load_companies()
    try:
        update_fundamentals(state, tickers)
    except (edgar_client.EdgarError, FundamentalsError) as exc:
        print(f"ERROR: {exc}\nNothing written.", file=sys.stderr)
        return 1
    score.rescore_all(state)
    store.save_companies(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
