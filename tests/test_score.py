import pytest

from screener import score


def _fundamentals(yoy=0.10, slope=0.0, margin=0.15, margin_chg=0.0, de=0.5, de_chg=0.0, cr=1.5):
    return {
        "latest": {"period_end": "2026-06-30", "revenue_yoy": yoy, "net_margin": margin,
                   "debt_to_equity": de, "current_ratio": cr},
        "trend": {"revenue_yoy_slope": slope, "net_margin_change_yoy": margin_chg,
                  "debt_to_equity_change_yoy": de_chg},
    }


def _filing(tone, *flags):
    return {"tone": tone, "red_flags": [{"category": c, "summary": ""} for c in flags]}


def test_weights_sum_to_one():
    assert sum(score.WEIGHTS.values()) == pytest.approx(1.0)
    assert sum(score.QUANT_WEIGHTS.values()) == pytest.approx(1.0)
    assert sum(score.QUAL_WEIGHTS.values()) == pytest.approx(1.0)


@pytest.mark.parametrize("composite,rating", [(100, "Buy"), (65, "Buy"), (64.9, "Hold"),
                                              (40, "Hold"), (39.9, "Avoid"), (0, "Avoid")])
def test_band_thresholds(composite, rating):
    assert score.band(composite) == rating


def test_interp_clamps_and_interpolates():
    anchors = [(0.0, 0), (1.0, 100)]
    assert score.interp(-5, anchors) == 0
    assert score.interp(0.25, anchors) == 25
    assert score.interp(9, anchors) == 100


def test_strong_company_rates_buy():
    rec = {
        "fundamentals": _fundamentals(yoy=0.30, slope=0.02, margin=0.35, margin_chg=0.03, de=0.2, cr=2.5),
        "qualitative": {"filings": [_filing("neutral"), _filing("bullish")]},
    }
    result = score.score_company(rec)
    assert result["rating"] == "Buy"
    assert result["rationale"].startswith("Buy — ")


def test_weak_company_rates_avoid():
    rec = {
        "fundamentals": _fundamentals(yoy=-0.12, slope=-0.03, margin=-0.05, margin_chg=-0.04, de=2.8,
                                      de_chg=0.4, cr=0.8),
        "qualitative": {"filings": [_filing("bearish", "demand_weakness"),
                                    _filing("bearish", "demand_weakness", "liquidity_debt")]},
    }
    result = score.score_company(rec)
    assert result["rating"] == "Avoid"


def test_middling_company_rates_hold_with_spec_style_rationale():
    rec = {
        "fundamentals": _fundamentals(yoy=0.05, margin=0.12, margin_chg=0.02, de=1.0, cr=1.3),
        "qualitative": {"filings": [_filing("neutral"), _filing("bearish"), _filing("bearish")]},
    }
    result = score.score_company(rec)
    assert result["rating"] == "Hold"
    assert result["rationale"] == (
        "Hold — margin improving but tone bearish for the 2nd consecutive filing."
    )


def test_composite_is_70_30_blend():
    rec = {"fundamentals": _fundamentals(), "qualitative": {"filings": [_filing("neutral")]}}
    result = score.score_company(rec)
    expected = 0.7 * result["quant"]["score"] + 0.3 * result["qualitative"]["score"]
    assert result["composite"] == pytest.approx(expected, abs=0.1)


def test_persistent_red_flag_scores_worse_than_new_one():
    new = score.qualitative_score([_filing("neutral"), _filing("neutral", "supply_chain")])
    persistent = score.qualitative_score(
        [_filing("neutral", "supply_chain"), _filing("neutral", "supply_chain"), _filing("neutral", "supply_chain")]
    )
    assert persistent["parts"]["red_flags"] < new["parts"]["red_flags"]


def test_tone_shift_to_bearish_counts_against_and_recovery_counts_for():
    steady = score.qualitative_score([_filing("neutral"), _filing("neutral")])
    worse = score.qualitative_score([_filing("neutral"), _filing("bearish")])
    better = score.qualitative_score([_filing("bearish"), _filing("neutral")])
    assert worse["parts"]["tone"] < steady["parts"]["tone"] < better["parts"]["tone"]


def test_missing_qualitative_uses_quant_only_and_says_so():
    rec = {"fundamentals": _fundamentals()}
    result = score.score_company(rec)
    assert result["qualitative"] is None
    assert result["composite"] == result["quant"]["score"]
    assert result["rationale"].endswith("(qualitative analysis pending).")


def test_missing_metric_is_neutral_and_reported():
    result = score.quant_score(_fundamentals(de=None, de_chg=None))
    assert result["parts"]["leverage"] == score.NEUTRAL
    assert "debt_to_equity" in result["missing"]


def test_momentum_is_not_an_input():
    rec = {"fundamentals": _fundamentals(), "momentum": {"vs_200dma": 0.5, "rel_universe": 0.3}}
    assert score.score_company(rec)["composite"] == score.score_company({"fundamentals": _fundamentals()})["composite"]


def test_rating_history_appends_only_on_change():
    state = {"companies": {"X": {"fundamentals": _fundamentals()}}}
    score.rescore_all(state)
    score.rescore_all(state)
    assert len(state["companies"]["X"]["rating_history"]) == 1
    state["companies"]["X"]["fundamentals"] = _fundamentals(yoy=-0.2, margin=-0.1, de=3, cr=0.5)
    score.rescore_all(state)
    assert [h["rating"] for h in state["companies"]["X"]["rating_history"]] == ["Hold", "Avoid"]
