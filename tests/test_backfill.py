import json

from screener import backfill, edgar_client, fetch_fundamentals, store
from screener.universe import tickers


def test_merge_seeds_everything(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(store, "FILINGS_SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(edgar_client, "resolve_ciks", lambda ts: {t: f"{i:010d}" for i, t in enumerate(ts)})
    feed = {"filings": {"recent": {
        "accessionNumber": ["k-1", "8k-1"], "form": ["10-K", "8-K"], "filingDate": ["2026-02-20", "2026-01-28"],
        "reportDate": ["2026-01-25", ""], "primaryDocument": ["a.htm", "b.htm"], "items": ["", "2.02,9.01"],
    }}}
    monkeypatch.setattr(edgar_client, "get_submissions", lambda cik: feed)

    def fake_fundamentals(state, ts):
        for t in ts:
            state["companies"][t]["fundamentals"] = {
                "latest": {"period_end": "2026-01-25", "revenue_yoy": 0.1, "net_margin": 0.15,
                           "debt_to_equity": 0.5, "current_ratio": 1.5},
                "trend": {"revenue_yoy_slope": 0.0, "net_margin_change_yoy": 0.0, "debt_to_equity_change_yoy": 0.0},
            }

    monkeypatch.setattr(fetch_fundamentals, "update_fundamentals", fake_fundamentals)
    partial = tmp_path / "NVDA.json"
    partial.write_text(json.dumps({"ticker": "NVDA", "results": [
        {"accession": "k-1", "form": "10-K", "filed": "2026-02-20", "period_end": "2026-01-25",
         "status": "ok", "tone": "bullish", "red_flags": []}]}))

    assert backfill.main(["merge", str(partial)]) == 0

    companies = {c["ticker"]: c for c in json.loads((tmp_path / "companies.json").read_text())["companies"]}
    assert set(companies) == set(tickers())
    assert companies["NVDA"]["score"]["qualitative"]["tone"] == "bullish"
    assert companies["AAPL"]["score"]["rationale"].endswith("(qualitative analysis pending).")
    assert companies["NVDA"]["recent_filings"][1]["items"] == ["Results of operations"]
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert seen["NVDA"]["accessions"] == ["k-1", "8k-1"]
