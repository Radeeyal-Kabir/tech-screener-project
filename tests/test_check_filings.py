import json

import pytest

from screener import analyze_filing, check_filings, edgar_client, fetch_fundamentals, store

CIKS = {"NVDA": "0001045810", "AAPL": "0000320193"}


def _feed(*filings):
    keys = {"accessionNumber": [], "form": [], "filingDate": [], "reportDate": [], "primaryDocument": [], "items": []}
    for acc, form, filed, items in filings:
        keys["accessionNumber"].append(acc)
        keys["form"].append(form)
        keys["filingDate"].append(filed)
        keys["reportDate"].append(filed)
        keys["primaryDocument"].append("doc.htm")
        keys["items"].append(items)
    return {"filings": {"recent": keys}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(store, "FILINGS_SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(edgar_client, "resolve_ciks", lambda ts: {t: CIKS[t] for t in ts})
    feeds = {cik: _feed(("old-1", "10-Q", "2026-05-01", "")) for cik in CIKS.values()}
    monkeypatch.setattr(edgar_client, "get_submissions", lambda cik: feeds[cik])
    calls = {"fundamentals": [], "analyze": []}

    def fake_fundamentals(state, tickers):
        calls["fundamentals"].extend(tickers)
        for t in tickers:
            state["companies"][t]["fundamentals"] = {
                "latest": {"period_end": "2026-07-26", "revenue_yoy": 0.2, "net_margin": 0.3,
                           "debt_to_equity": 0.2, "current_ratio": 2.0},
                "trend": {"revenue_yoy_slope": 0.0, "net_margin_change_yoy": 0.0, "debt_to_equity_change_yoy": 0.0},
            }

    def fake_analyze(ticker, cik, filing):
        calls["analyze"].append((ticker, filing["accession"]))
        return {"accession": filing["accession"], "form": filing["form"], "filed": filing["filed"],
                "period_end": filing["period_end"], "status": "ok", "tone": "neutral", "red_flags": []}

    monkeypatch.setattr(fetch_fundamentals, "update_fundamentals", fake_fundamentals)
    monkeypatch.setattr(analyze_filing, "analyze_filing", fake_analyze)
    return {"feeds": feeds, "calls": calls, "tmp": tmp_path}


def _seen(env):
    return json.loads((env["tmp"] / "seen.json").read_text())


def _companies(env):
    return {c["ticker"]: c for c in json.loads((env["tmp"] / "companies.json").read_text())["companies"]}


def test_first_run_initializes_without_processing(env):
    assert check_filings.main(["NVDA", "AAPL"]) == 0
    assert _seen(env)["NVDA"]["accessions"] == ["old-1"]
    assert env["calls"]["fundamentals"] == [] and env["calls"]["analyze"] == []
    assert not (env["tmp"] / "companies.json").exists()


def test_new_8k_is_recorded_as_event_only(env):
    check_filings.main(["NVDA", "AAPL"])
    env["feeds"][CIKS["NVDA"]] = _feed(("8k-1", "8-K", "2026-08-27", "2.02,9.01"), ("old-1", "10-Q", "2026-05-01", ""))
    assert check_filings.main(["NVDA", "AAPL"]) == 0
    nvda = _companies(env)["NVDA"]
    assert nvda["recent_filings"][0]["items"] == ["Results of operations"]
    assert env["calls"]["fundamentals"] == []
    assert _seen(env)["NVDA"]["accessions"][0] == "8k-1"


def test_new_10q_refreshes_only_that_company(env):
    check_filings.main(["NVDA", "AAPL"])
    env["feeds"][CIKS["NVDA"]] = _feed(("q-2", "10-Q", "2026-08-27", ""), ("old-1", "10-Q", "2026-05-01", ""))
    assert check_filings.main(["NVDA", "AAPL"]) == 0
    assert env["calls"]["fundamentals"] == ["NVDA"]
    assert env["calls"]["analyze"] == [("NVDA", "q-2")]
    nvda = _companies(env)["NVDA"]
    assert nvda["score"]["rating"] in {"Buy", "Hold", "Avoid"}
    assert nvda["qualitative"]["filings"][-1]["accession"] == "q-2"


def test_failure_saves_finished_companies_and_retries_the_rest(env, monkeypatch):
    check_filings.main(["NVDA", "AAPL"])
    for cik in CIKS.values():
        env["feeds"][cik] = _feed(("q-2" + cik, "10-Q", "2026-08-27", ""), ("old-1", "10-Q", "2026-05-01", ""))
    real = analyze_filing.analyze_filing

    def flaky(ticker, cik, filing):
        if ticker == "AAPL":
            raise analyze_filing.QualitativeError("Ollama not reachable")
        return real(ticker, cik, filing)

    monkeypatch.setattr(analyze_filing, "analyze_filing", flaky)
    assert check_filings.main(["NVDA", "AAPL"]) == 1
    seen = _seen(env)
    assert seen["NVDA"]["accessions"][0].startswith("q-2")
    assert seen["AAPL"]["accessions"] == ["old-1"]  # not advanced: retried next run
    companies = _companies(env)
    assert "score" in companies["NVDA"]
    assert "fundamentals" not in companies["AAPL"]  # rolled back


@pytest.mark.parametrize("form,expected", [("10-K", "true"), ("8-K", "false")])
def test_detect_sets_needs_llm(env, monkeypatch, tmp_path, form, expected):
    check_filings.main(["NVDA", "AAPL"])
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    env["feeds"][CIKS["AAPL"]] = _feed(("n-1", form, "2026-10-30", ""), ("old-1", "10-Q", "2026-05-01", ""))
    assert check_filings.main(["--detect", "NVDA", "AAPL"]) == 0
    assert out.read_text().strip() == f"needs_llm={expected}"
    assert _seen(env)["AAPL"]["accessions"] == ["old-1"]  # detect never writes state
