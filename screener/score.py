"""The composite scoring formula: the whole methodology lives in this file.

    composite (0-100) = 70% x quant score + 30% x qualitative score

Quant score (from SEC XBRL fundamentals, see fetch_fundamentals.py):
    revenue growth  35%  latest YoY growth (70%) + direction over last 4 quarters (30%)
    net margin      30%  latest margin level (70%) + change vs. same quarter last year (30%)
    leverage        20%  debt/equity level (70%) + change vs. a year ago (30%)
    liquidity       15%  current ratio

Qualitative score (from the MD&A analysis, see analyze_filing.py):
    tone            50%  latest management tone, adjusted for the direction of change
                         and for consecutive bearish filings
    red flags       50%  starts at 100; each current red flag costs more the longer
                         it has persisted across consecutive filings

Price momentum is deliberately NOT an input. It is computed and shown separately
(fetch_prices.py) so the dashboard's "price vs. rating" tension view compares two
independent signals instead of a score that already partly reflects the price.

Every sub-score maps a raw metric to 0-100 by linear interpolation between the
anchor points below, clamped at the ends. Missing inputs score a neutral 50 and
are listed in the output, so a gap never silently helps or hurts a company.
If no filing has been analyzed yet, the composite is the quant score alone.

Bands: composite >= 65 -> Buy, < 40 -> Avoid, otherwise Hold.
"""

from __future__ import annotations

from screener.store import utc_now_iso

WEIGHTS = {"quant": 0.70, "qualitative": 0.30}

QUANT_WEIGHTS = {"revenue_growth": 0.35, "net_margin": 0.30, "leverage": 0.20, "liquidity": 0.15}
QUAL_WEIGHTS = {"tone": 0.50, "red_flags": 0.50}

LEVEL_VS_TREND = (0.70, 0.30)

# (raw value, score) anchor points. Raw values are fractions: 0.10 = 10%.
ANCHORS = {
    "revenue_yoy": [(-0.10, 0), (0.0, 30), (0.10, 60), (0.25, 100)],
    "revenue_yoy_slope": [(-0.05, 0), (0.0, 50), (0.05, 100)],  # change in YoY growth per quarter
    "net_margin": [(-0.10, 0), (0.0, 20), (0.10, 50), (0.25, 80), (0.40, 100)],
    "net_margin_change_yoy": [(-0.05, 0), (0.0, 50), (0.05, 100)],
    "debt_to_equity": [(0.0, 100), (0.5, 80), (1.0, 60), (2.0, 30), (3.0, 0)],
    "debt_to_equity_change_yoy": [(-0.5, 100), (0.0, 50), (0.5, 0)],
    "current_ratio": [(0.5, 0), (1.0, 40), (1.5, 70), (2.5, 100)],
}

TONE_LEVEL = {"bullish": 75, "neutral": 50, "bearish": 25}
TONE_ORDER = {"bearish": -1, "neutral": 0, "bullish": 1}
TONE_SHIFT_POINTS = 15  # per step of change vs. the previous filing
CONSECUTIVE_BEARISH_PENALTY = 10  # per additional consecutive bearish filing

# Cost of one red flag, by how many consecutive filings (including this one) it has appeared in.
RED_FLAG_COST = {1: 10, 2: 20}
RED_FLAG_COST_PERSISTENT = 30  # 3+ consecutive filings

BANDS = [(65, "Buy"), (40, "Hold"), (0, "Avoid")]

NEUTRAL = 50.0


def interp(x: float, anchors: list[tuple[float, float]]) -> float:
    pts = sorted(anchors)
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    raise AssertionError("unreachable")


def _metric(value: float | None, anchor_key: str, missing: list[str]) -> float:
    if value is None:
        missing.append(anchor_key)
        return NEUTRAL
    return interp(value, ANCHORS[anchor_key])


def _level_trend(level: float, trend: float) -> float:
    return LEVEL_VS_TREND[0] * level + LEVEL_VS_TREND[1] * trend


def quant_score(fundamentals: dict) -> dict:
    latest, trend = fundamentals["latest"], fundamentals["trend"]
    missing: list[str] = []
    parts = {
        "revenue_growth": _level_trend(
            _metric(latest["revenue_yoy"], "revenue_yoy", missing),
            _metric(trend["revenue_yoy_slope"], "revenue_yoy_slope", missing),
        ),
        "net_margin": _level_trend(
            _metric(latest["net_margin"], "net_margin", missing),
            _metric(trend["net_margin_change_yoy"], "net_margin_change_yoy", missing),
        ),
        "leverage": _level_trend(
            _metric(latest["debt_to_equity"], "debt_to_equity", missing),
            _metric(trend["debt_to_equity_change_yoy"], "debt_to_equity_change_yoy", missing),
        ),
        "liquidity": _metric(latest["current_ratio"], "current_ratio", missing),
    }
    total = sum(QUANT_WEIGHTS[k] * v for k, v in parts.items())
    return {"score": round(total, 1), "parts": {k: round(v, 1) for k, v in parts.items()}, "missing": missing}


def _consecutive_bearish(filings: list[dict]) -> int:
    n = 0
    for f in reversed(filings):
        if f.get("tone") != "bearish":
            break
        n += 1
    return n


def _flag_streak(category: str, filings: list[dict]) -> int:
    """How many consecutive filings, ending with the latest, raised this category."""
    n = 0
    for f in reversed(filings):
        if category not in {rf["category"] for rf in f.get("red_flags", [])}:
            break
        n += 1
    return n


def qualitative_score(filings: list[dict]) -> dict | None:
    """``filings`` is oldest -> newest. Returns None if nothing analyzed yet."""
    filings = [f for f in filings if f.get("tone") in TONE_LEVEL]
    if not filings:
        return None
    latest = filings[-1]

    tone = float(TONE_LEVEL[latest["tone"]])
    tone_shift = 0
    if len(filings) >= 2:
        tone_shift = TONE_ORDER[latest["tone"]] - TONE_ORDER[filings[-2]["tone"]]
        tone += TONE_SHIFT_POINTS * tone_shift
    bearish_run = _consecutive_bearish(filings)
    tone -= CONSECUTIVE_BEARISH_PENALTY * max(0, bearish_run - 1)
    tone = max(0.0, min(100.0, tone))

    flags = []
    cost = 0
    for category in sorted({rf["category"] for rf in latest.get("red_flags", [])}):
        streak = _flag_streak(category, filings)
        cost += RED_FLAG_COST.get(streak, RED_FLAG_COST_PERSISTENT)
        flags.append({"category": category, "consecutive_filings": streak})
    red_flags = max(0.0, 100.0 - cost)

    total = QUAL_WEIGHTS["tone"] * tone + QUAL_WEIGHTS["red_flags"] * red_flags
    return {
        "score": round(total, 1),
        "parts": {"tone": round(tone, 1), "red_flags": round(red_flags, 1)},
        "tone": latest["tone"],
        "tone_shift": tone_shift,
        "consecutive_bearish": bearish_run,
        "flags": flags,
    }


def band(composite: float) -> str:
    for threshold, rating in BANDS:
        if composite >= threshold:
            return rating
    return BANDS[-1][1]


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def rationale(rating: str, fundamentals: dict, quant: dict, qual: dict | None) -> str:
    """One-line, rule-based explanation: the strongest positive and negative driver."""
    latest, trend = fundamentals["latest"], fundamentals["trend"]
    pos: list[tuple[float, str]] = []
    neg: list[tuple[float, str]] = []

    def add(strength: float, positive_text: str, negative_text: str) -> None:
        # strength: sub-score distance from neutral, so the most extreme driver wins.
        (pos if strength > 0 else neg).append((abs(strength), positive_text if strength > 0 else negative_text))

    if latest["revenue_yoy"] is not None:
        add(quant["parts"]["revenue_growth"] - NEUTRAL,
            f"revenue up {latest['revenue_yoy']:.0%} YoY",
            f"revenue {'down' if latest['revenue_yoy'] < 0 else 'up only'} {abs(latest['revenue_yoy']):.0%} YoY")
    if trend["net_margin_change_yoy"] is not None and abs(trend["net_margin_change_yoy"]) >= 0.01:
        add(interp(trend["net_margin_change_yoy"], ANCHORS["net_margin_change_yoy"]) - NEUTRAL,
            "margin improving", "margin shrinking")
    elif latest["net_margin"] is not None:
        add(interp(latest["net_margin"], ANCHORS["net_margin"]) - NEUTRAL,
            f"{latest['net_margin']:.0%} net margin", f"thin {latest['net_margin']:.0%} net margin")
    add(quant["parts"]["leverage"] - NEUTRAL, "low leverage", "leverage elevated")
    add(quant["parts"]["liquidity"] - NEUTRAL, "strong liquidity", "tight liquidity")

    if qual is not None:
        if qual["consecutive_bearish"] >= 2:
            neg.append((40, f"tone bearish for the {_ordinal(qual['consecutive_bearish'])} consecutive filing"))
        elif qual["tone_shift"] < 0:
            neg.append((30, f"tone turned {qual['tone']}"))
        elif qual["tone_shift"] > 0:
            pos.append((30, f"tone recovering to {qual['tone']}"))
        persistent = [f for f in qual["flags"] if f["consecutive_filings"] >= 2]
        if persistent:
            worst = max(persistent, key=lambda f: f["consecutive_filings"])
            neg.append((35, f"{worst['category'].replace('_', ' ')} flagged "
                            f"{worst['consecutive_filings']} filings running"))

    pos.sort(reverse=True)
    neg.sort(reverse=True)
    top_pos = pos[0][1] if pos else None
    top_neg = neg[0][1] if neg else None

    def join(lead: str | None, other: str | None, word: str) -> str:
        if lead and other:
            return f"{lead}{word}{other}"
        return lead or other or "mixed signals"

    if rating == "Buy":
        body = join(top_pos, top_neg, ", though ")
    elif rating == "Avoid":
        body = join(top_neg, top_pos, ", despite ")
    else:
        body = join(top_pos, top_neg, " but ")
    return f"{rating} — {body}."


def score_company(record: dict) -> dict | None:
    fundamentals = record.get("fundamentals")
    if not fundamentals:
        return None
    quant = quant_score(fundamentals)
    qual = qualitative_score(record.get("qualitative", {}).get("filings", []))
    if qual is None:
        composite = quant["score"]
    else:
        composite = WEIGHTS["quant"] * quant["score"] + WEIGHTS["qualitative"] * qual["score"]
    composite = round(composite, 1)
    rating = band(composite)
    text = rationale(rating, fundamentals, quant, qual)
    if qual is None:
        text = text[:-1] + " (qualitative analysis pending)."
    return {
        "composite": composite,
        "rating": rating,
        "rationale": text,
        "quant": quant,
        "qualitative": qual,
        "weights": WEIGHTS,
    }


def rescore_all(state: dict) -> None:
    """Recompute every company's score in place and append to its rating
    history whenever the rating changes (the dashboard uses this for
    "rating changed recently" in the tension view)."""
    now = utc_now_iso()
    for rec in state["companies"].values():
        result = score_company(rec)
        if result is None:
            continue
        rec["score"] = {**result, "scored_at": now}
        history = rec.setdefault("rating_history", [])
        if not history or history[-1]["rating"] != result["rating"]:
            history.append({"at": now, "rating": result["rating"], "composite": result["composite"]})
