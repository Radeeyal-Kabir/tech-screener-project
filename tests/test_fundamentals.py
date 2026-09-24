from datetime import date, timedelta

import pytest

from screener.fetch_fundamentals import (
    FundamentalsError,
    compute_fundamentals,
    quarterly_flow_series,
)

# A fiscal year ending late September, 13-week quarters (91 days), 364-day years.
FY_ENDS = [date(2021, 9, 25) + timedelta(days=364 * i) for i in range(4)]


def _quarters(fy_end: date) -> list[tuple[date, date]]:
    start = fy_end - timedelta(days=363)
    out = []
    for _ in range(4):
        end = start + timedelta(days=90)
        out.append((start, end))
        start = end + timedelta(days=1)
    out[-1] = (out[-1][0], fy_end)
    return out


def _flow(values_by_fy: dict[date, list[float]], *, filed_offset=40) -> list[dict]:
    """10-Q facts for Q1-Q3, and only the annual total in the 10-K (like real filers)."""
    facts = []
    for fy_end, qvals in values_by_fy.items():
        qs = _quarters(fy_end)
        for (s, e), v in list(zip(qs, qvals))[:3]:
            facts.append({"start": s.isoformat(), "end": e.isoformat(), "val": v, "form": "10-Q",
                          "filed": (e + timedelta(days=filed_offset)).isoformat()})
        facts.append({"start": qs[0][0].isoformat(), "end": fy_end.isoformat(), "val": sum(qvals),
                      "form": "10-K", "filed": (fy_end + timedelta(days=60)).isoformat()})
    return facts


def _instant(value_fn, *, fy_ends=FY_ENDS) -> list[dict]:
    facts = []
    for fy_end in fy_ends:
        for _, e in _quarters(fy_end):
            facts.append({"end": e.isoformat(), "val": value_fn(e), "form": "10-Q",
                          "filed": (e + timedelta(days=40)).isoformat()})
    return facts


def _companyfacts(concepts: dict[str, list[dict]]) -> dict:
    return {"facts": {"us-gaap": {k: {"units": {"USD": v}} for k, v in concepts.items()}}}


REVENUE = {fy: [100 * (1.1 ** i), 110 * (1.1 ** i), 120 * (1.1 ** i), 150 * (1.1 ** i)]
           for i, fy in enumerate(FY_ENDS)}
NET_INCOME = {fy: [v * 0.2 for v in qs] for fy, qs in REVENUE.items()}


def _base(**overrides):
    concepts = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": _flow(REVENUE),
        "NetIncomeLoss": _flow(NET_INCOME),
        "AssetsCurrent": _instant(lambda e: 300.0),
        "LiabilitiesCurrent": _instant(lambda e: 150.0),
        "StockholdersEquity": _instant(lambda e: 500.0),
        "LongTermDebt": _instant(lambda e: 250.0),
    }
    concepts.update(overrides)
    return _companyfacts({k: v for k, v in concepts.items() if v is not None})


TODAY = FY_ENDS[-1] + timedelta(days=30)


def test_q4_derived_from_annual_minus_q1_to_q3():
    series = quarterly_flow_series(_flow(REVENUE))
    fy = FY_ENDS[-1]
    assert series[fy] == pytest.approx(REVENUE[fy][3])
    assert len(series) == 16


def test_yoy_growth_margin_and_ratios():
    out = compute_fundamentals(_base(), today=TODAY)
    assert len(out["quarters"]) == 8
    latest = out["latest"]
    assert latest["period_end"] == FY_ENDS[-1].isoformat()
    assert latest["revenue_yoy"] == pytest.approx(0.10, abs=1e-4)
    assert latest["net_margin"] == pytest.approx(0.20, abs=1e-4)
    assert latest["debt_to_equity"] == pytest.approx(0.5)
    assert latest["current_ratio"] == pytest.approx(2.0)
    assert out["trend"]["net_margin_change_yoy"] == pytest.approx(0.0, abs=1e-4)
    assert out["warnings"] == []


def test_restated_value_wins():
    facts = _flow(REVENUE)
    q1 = next(f for f in facts if f["form"] == "10-Q" and f["end"] == _quarters(FY_ENDS[-1])[0][1].isoformat())
    facts.append({**q1, "val": 999.0, "filed": "2030-01-01"})
    series = quarterly_flow_series(facts)
    assert series[_quarters(FY_ENDS[-1])[0][1]] == 999.0


def test_concept_switch_merges_old_and_new_tags():
    old_fys, new_fys = FY_ENDS[:2], FY_ENDS[2:]
    out = compute_fundamentals(
        _base(
            RevenueFromContractWithCustomerExcludingAssessedTax=_flow({fy: REVENUE[fy] for fy in new_fys}),
            Revenues=_flow({fy: REVENUE[fy] for fy in old_fys}),
        ),
        today=TODAY,
    )
    assert out["concepts_used"]["revenue"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    # YoY for the first new-tag quarter needs the old-tag quarter a year earlier.
    assert all(q["revenue_yoy"] == pytest.approx(0.10, abs=1e-4) for q in out["quarters"])


def test_missing_required_concept_fails_loudly():
    with pytest.raises(FundamentalsError, match="net_income"):
        compute_fundamentals(_base(NetIncomeLoss=None), today=TODAY)


def test_negative_equity_blanks_debt_to_equity():
    out = compute_fundamentals(_base(StockholdersEquity=_instant(lambda e: -50.0)), today=TODAY)
    assert out["latest"]["debt_to_equity"] is None
    assert any("Negative stockholders' equity" in w for w in out["warnings"])


def test_debt_falls_back_to_noncurrent_plus_current():
    out = compute_fundamentals(
        _base(
            LongTermDebt=None,
            LongTermDebtNoncurrent=_instant(lambda e: 200.0),
            LongTermDebtCurrent=_instant(lambda e: 50.0),
        ),
        today=TODAY,
    )
    assert out["latest"]["debt_to_equity"] == pytest.approx(0.5)


def test_stale_data_warns():
    out = compute_fundamentals(_base(), today=TODAY + timedelta(days=400))
    assert any("over 200 days old" in w for w in out["warnings"])
