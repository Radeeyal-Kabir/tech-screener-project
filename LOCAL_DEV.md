# Local development

Setup and workflow for working on this repo locally — most useful for
anything that needs a fast iterate-and-inspect loop (prompt tuning, MD&A
extraction, scoring logic) that a cloud session can't give you, since this
repo's CI environment can't reach Ollama or SEC EDGAR.

## One-time setup

```bash
git clone https://github.com/Radeeyal-Kabir/tech-screener-project.git
cd tech-screener-project

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export EDGAR_USER_AGENT="Your Name you@example.com"   # SEC requires a real contact
```

### Ollama (for the qualitative pipeline)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &                      # leave running in the background
ollama pull llama3.2:3b             # matches OLLAMA_MODEL in .env.example / the workflows
```

Keep the model at the 1–3B class — that's a deliberate choice so the
qualitative pipeline runs free on GitHub Actions' CPU-only runners (see
`.github/actions/setup-ollama/action.yml`). Don't swap in a bigger model as
part of a "fix"; if a bigger model changes behavior, that's a finding to
report, not something to silently adopt.

## Running things

```bash
python -m pytest                          # unit tests, no network needed
python -m screener.fetch_prices           # writes data/prices.json
python -m screener.fetch_fundamentals     # writes data/companies.json
python -m screener.check_filings          # polls EDGAR, processes any new filing

# Analyze one filing — this is the tight loop for extraction work:
python -m screener.analyze_filing TICKER                 # latest 10-Q/10-K
python -m screener.analyze_filing TICKER ACCESSION-NUMBER # a specific one

bash frontend/build.sh && python -m http.server -d frontend 8000  # dashboard at localhost:8000
```

`analyze_filing.py`'s `main()` prints the result JSON (minus red_flags, to
keep it short) to stderr and also writes it into `data/companies.json` under
that company's `qualitative.filings`. For iterating on the extraction logic
itself (`select_text`, `chunk`, the prompt, `quote_in_text`), it's usually
faster to import the functions directly in a scratch script or a REPL and
run them against a saved filing's text than to round-trip through the CLI
each time — `extract_mda(cik10, filing_dict)` and `analyze_chunk(text,
context)` are the two functions worth calling directly.

## The data already in this repo

`data/companies.json` and `data/prices.json` are committed, real output from
a full backfill run (`.github/workflows/backfill.yml`, run once via
`workflow_dispatch` on 2026-09-25) — not synthetic fixtures. Every company
has 8 quarters of fundamentals and up to 8 analyzed filings under
`companies[].qualitative.filings`. That existing data is itself the best
starting point for debugging extraction quality: read a company's flagged
red flags and their `quote` fields before pulling any new filing, since the
verbatim quotes are already sitting there next to the filing's accession
number and `source_url`.

## Conventions

- This is a solo project; work happens directly on `main` (no PR review
  gate). Commit only once a fix is verified against real output, not
  mid-experiment.
- Never touch `data/state/filings_seen.json` by hand — it drives what the
  filing watcher treats as "already processed."
- `python -m pytest` should stay green. Extend
  `tests/test_analyze_filing.py` (it already tests `find_section`,
  `select_text`, `quote_in_text`, `combine`, etc. against synthetic HTML) as
  part of any extraction fix, not just eyeballing real output.
