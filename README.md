# S&P 500 Tech Screener

A live, automated screener covering ~20 S&P 500 Information Technology
companies. It combines SEC EDGAR fundamental data (quantitative) with
LLM-extracted filing red flags and management tone (qualitative) into one
composite score per company — **Buy / Hold / Avoid**, with a plain-language
rationale.

This is a personal project built to demonstrate an end-to-end automated data
pipeline: SEC EDGAR ingestion, event-driven scheduling, a transparent scoring
methodology, and a static dashboard, all running on free infrastructure.

> This is a personal project, not investment advice.

## How it works

Two independent GitHub Actions workflows keep the data fresh without any
server or database:

- **Daily price job** (weekdays, after US market close) — pulls OHLCV for
  all tracked tickers from Stooq, computes % change, updates the
  winners/losers ticker data.
- **Filing check job** (every 6 hours) — polls each company's SEC EDGAR
  submissions feed for a new 10-Q/10-K/8-K. On a hit, it re-pulls that one
  company's XBRL fundamentals and MD&A text, runs the qualitative
  extraction, and recomputes its composite score.

Both jobs commit their output as JSON under `data/`, and the frontend
(deployed on Cloudflare Pages) reads those files directly and redeploys
automatically on every push.

## Scoring methodology

The composite score is **quant-led (70%) with qualitative filing analysis as
a modifier (30%)**:

- **Quant (70%)** — revenue growth trend, net margin level/trend,
  debt/equity trajectory, current ratio.
- **Qualitative (30%)** — management tone trend, red-flag persistence
  across filings.

Price momentum is tracked and displayed **separately** — it is deliberately
excluded from the composite score so the "score vs. price" tension view
isn't self-fulfilling. The full formula lives in
[`screener/score.py`](screener/score.py), documented and unit-tested, so the
rating for any company is inspectable rather than a black box.

## Repo layout

```
screener/
  universe.py           # the tracked companies (ticker, name, sub-sector)
  edgar_client.py        # shared SEC EDGAR HTTP client (User-Agent, retry/backoff, CIK lookup)
  fetch_fundamentals.py  # XBRL fundamentals -> quant ratios
  analyze_filing.py      # MD&A text -> tone / red-flag extraction (local Ollama)
  check_filings.py       # polls EDGAR submissions, diffs against last-seen accession numbers
  fetch_prices.py        # daily OHLCV from Stooq (yfinance fallback)
  score.py               # the composite scoring formula
  backfill.py            # seeds the dataset across the initial lookback window
data/
  companies.json          # per-company fundamentals, qualitative signals, score, rating
  prices.json              # daily prices + % change, winners/losers
  state/filings_seen.json  # last-seen accession number per CIK
tests/
  test_score.py
frontend/                 # static dashboard, reads data/*.json
.github/workflows/        # the two scheduled pipelines
```

## Data sources (all free, no paid keys)

| Data | Source |
| --- | --- |
| Fundamentals | SEC EDGAR `companyfacts` API (XBRL) |
| New-filing detection | SEC EDGAR `submissions` API per CIK |
| MD&A narrative text | SEC EDGAR full-text filing index |
| Daily prices | Stooq CSV endpoint (`yfinance` fallback) |
| Qualitative tone / red flags | Local LLM via Ollama (small quantized model) |

SEC EDGAR requires a `User-Agent` header identifying a real contact — set
`EDGAR_USER_AGENT` (see `.env.example`).

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in EDGAR_USER_AGENT

python -m screener.fetch_prices
python -m screener.fetch_fundamentals
python -m screener.check_filings
python -m pytest tests/
```

## Status

Under active build. See the build spec doc for the full deliverables list
and open decisions.
