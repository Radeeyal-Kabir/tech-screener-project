"""Filing watcher: poll EDGAR for new 10-K/10-Q/8-K filings and react.

Usage:
    python -m screener.check_filings            # detect + process new filings
    python -m screener.check_filings --detect   # detect only; prints and sets the
                                                # `needs_llm` GitHub Actions output

For each tracked company, the newest filings in its EDGAR submissions feed
are compared against the accession numbers already seen
(data/state/filings_seen.json):

  - new 10-K/10-Q -> refresh that company's XBRL fundamentals, analyze the
    filing's MD&A, rescore. Only this company is touched.
  - new 8-K       -> recorded as an event for the dashboard. 8-Ks carry no
    XBRL financials or MD&A, so they don't change the score.

A company seen for the first time is initialized (its current filings are
marked seen) without processing; history comes from the backfill.

If EDGAR or Ollama fails mid-run, companies already fully processed are
saved (each is internally consistent), the failing and remaining ones keep
their previous seen-state so the next run retries them, and the process
exits non-zero.
"""

from __future__ import annotations

import copy
import os
import sys

from screener import analyze_filing, edgar_client, fetch_fundamentals, score, store
from screener.universe import tickers as all_tickers

PERIODIC_FORMS = ("10-K", "10-Q")
WATCHED_FORMS = PERIODIC_FORMS + ("8-K",)
FEED_WINDOW = 15  # newest watched filings considered per run
SEEN_KEPT = 50
EVENTS_KEPT = 10

EIGHT_K_ITEMS = {
    "1.01": "Material agreement",
    "1.05": "Cybersecurity incident",
    "2.01": "Acquisition or disposition",
    "2.02": "Results of operations",
    "2.05": "Exit or restructuring costs",
    "2.06": "Material impairment",
    "4.02": "Non-reliance on prior financials",
    "5.02": "Officer or director change",
    "5.07": "Shareholder vote",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
}


def detect(tickers: list[str], seen: dict) -> tuple[dict[str, str], dict[str, list[dict] | None]]:
    """Return (ciks, {ticker: new filings oldest-first, or None if uninitialized})."""
    ciks = edgar_client.resolve_ciks(tickers)
    new: dict[str, list[dict] | None] = {}
    for t in tickers:
        feed = edgar_client.recent_filings(edgar_client.get_submissions(ciks[t]), WATCHED_FORMS)[:FEED_WINDOW]
        entry = seen.get(t)
        if entry is None:
            new[t] = None
            seen[t] = {"accessions": [f["accession"] for f in feed]}
            continue
        known = set(entry.get("accessions", []))
        new[t] = list(reversed([f for f in feed if f["accession"] not in known]))
    return ciks, new


def _event(cik10: str, f: dict) -> dict:
    codes = [c.strip() for c in (f.get("items") or "").split(",") if c.strip() and c.strip() != "9.01"]
    return {
        "form": f["form"],
        "filed": f["filed"],
        "accession": f["accession"],
        "items": [EIGHT_K_ITEMS.get(c, f"Item {c}") for c in codes],
        "url": edgar_client.archive_url(cik10, f["accession"], f["primary_document"]),
    }


def process(state: dict, seen: dict, ticker: str, cik10: str, new: list[dict]) -> None:
    rec = state["companies"][ticker]
    rec["cik"] = cik10
    events = [_event(cik10, f) for f in new]
    rec["recent_filings"] = sorted(events + rec.get("recent_filings", []),
                                   key=lambda e: (e["filed"], e["accession"]), reverse=True)[:EVENTS_KEPT]

    periodic = [f for f in new if f["form"] in PERIODIC_FORMS]
    if periodic:
        fetch_fundamentals.update_fundamentals(state, [ticker])
        for f in periodic:
            result = analyze_filing.analyze_filing(ticker, cik10, f)
            analyze_filing.record_result(state, ticker, result)

    known = seen.setdefault(ticker, {"accessions": []})["accessions"]
    seen[ticker]["accessions"] = ([f["accession"] for f in reversed(new)] + known)[:SEEN_KEPT]


def _set_github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{name}={value}\n")


def main(argv: list[str]) -> int:
    detect_only = "--detect" in argv
    tickers = [a.upper() for a in argv if not a.startswith("--")] or all_tickers()
    seen = store.read_json(store.FILINGS_SEEN_FILE, {})

    try:
        ciks, new = detect(tickers, seen)
    except edgar_client.EdgarError as exc:
        print(f"ERROR: {exc}\nNothing written.", file=sys.stderr)
        return 1

    initialized = [t for t, v in new.items() if v is None]
    pending = {t: v for t, v in new.items() if v}
    needs_llm = any(f["form"] in PERIODIC_FORMS for fs in pending.values() for f in fs)
    for t, fs in pending.items():
        print(f"{t}: new {', '.join(f['form'] + ' ' + f['filed'] for f in fs)}", file=sys.stderr)
    if initialized:
        print(f"Initialized (no processing): {', '.join(initialized)}", file=sys.stderr)
    if not pending:
        print("No new filings.", file=sys.stderr)

    if detect_only:
        _set_github_output("needs_llm", str(needs_llm).lower())
        print(f"needs_llm={str(needs_llm).lower()}")
        return 0

    state = store.load_companies()
    processed: list[str] = []
    failure: str | None = None
    for t, fs in pending.items():
        snapshot = copy.deepcopy(state["companies"][t])
        try:
            process(state, seen, t, ciks[t], fs)
        except (edgar_client.EdgarError, fetch_fundamentals.FundamentalsError,
                analyze_filing.QualitativeError) as exc:
            # Roll this company back; its seen-state wasn't advanced, so it's retried next run.
            state["companies"][t] = snapshot
            failure = f"{t}: {exc}"
            break
        processed.append(t)

    if processed:
        score.rescore_all(state)
        store.save_companies(state)
    if processed or initialized:
        store.write_json(store.FILINGS_SEEN_FILE, seen)

    if failure:
        print(f"ERROR: {failure}\nSaved {len(processed)} fully processed company(ies); "
              "the rest will be retried next run.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
