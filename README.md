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

## Setup (one time)

1. **Keep the repo public.** Public repos get unlimited GitHub Actions minutes
   on standard runners; private repos are capped at 2,000/month.
2. **Add the SEC contact secret.** Settings → Secrets and variables →
   Actions → New repository secret: `EDGAR_USER_AGENT` = `Your Name you@example.com`.
3. **Workflow permissions.** The workflows declare `contents: write`
   themselves; if your account or org restricts `GITHUB_TOKEN` to read-only,
   allow read/write under Settings → Actions → General → Workflow permissions.
4. **Seed the data.** Actions → Backfill → Run workflow. It analyzes each
   company's last 8 10-K/10-Q filings in parallel (one job per company,
   roughly 30–45 minutes), then commits fundamentals, scores and prices.
   Alternatively run it locally with Ollama: `python -m screener.backfill all`.
5. **Deploy the site on Cloudflare Pages.** Workers & Pages → Create →
   Pages → connect this repo. Framework preset: None. Build command:
   `bash frontend/build.sh`. Build output directory: `frontend`. Every data
   commit from the scheduled jobs then redeploys automatically.

Scheduled workflows only run from the default branch. GitHub also pauses
schedules in public repos after 60 days without repository activity. The
daily price commits count as activity, but if the site ever looks stale,
check the Actions tab.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export EDGAR_USER_AGENT="Your Name you@example.com"

python -m pytest                       # unit tests, no network needed
python -m screener.fetch_prices        # writes data/prices.json
python -m screener.fetch_fundamentals  # writes data/companies.json
python -m screener.analyze_filing NVDA # needs `ollama serve` + `ollama pull llama3.2:3b`
python -m screener.check_filings

bash frontend/build.sh && python -m http.server -d frontend 8000  # dashboard at localhost:8000
```

## Known limitations

- The qualitative signal comes from a small (3B) model reading a selection
  of the MD&A, not the whole section, so treat tone as a coarse signal. Red
  flags are only kept when the model's quote appears verbatim in the filing.
- MD&A section detection is heuristic. If it can't find the section, the
  filing is marked `extraction_failed`, the run shows a warning, and the
  score falls back to fundamentals for that filing.
- 8-K filings are shown as events but don't change the score (they carry no
  XBRL financials or MD&A).
