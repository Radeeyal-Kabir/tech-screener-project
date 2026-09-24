"""Seed the dataset before the scheduled jobs take over.

Usage:
    # Everything, sequentially (for a machine with Ollama running locally):
    python -m screener.backfill all

    # What the backfill GitHub Actions workflow does, split so the slow
    # qualitative part runs as one parallel job per company:
    python -m screener.backfill qualitative NVDA --out partials/NVDA.json
    python -m screener.backfill merge partials/*.json

``qualitative`` analyzes a company's last FILINGS_KEPT 10-K/10-Q filings
(the same 8-quarter lookback the fundamentals use) and writes the results
to its own file. ``merge`` then refreshes fundamentals for every company,
folds in the partial results, records recent filings as events, marks all
current filings as seen (so the watcher doesn't redo them), rescores, and
saves. ``all`` does both in one process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from screener import analyze_filing, check_filings, edgar_client, fetch_fundamentals, score, store
from screener.universe import tickers as all_tickers


def qualitative_for(ticker: str) -> list[dict]:
    cik10 = edgar_client.resolve_ciks([ticker])[ticker]
    feed = edgar_client.recent_filings(edgar_client.get_submissions(cik10), analyze_filing.PERIODIC_FORMS)
    filings = list(reversed(feed[: analyze_filing.FILINGS_KEPT]))  # oldest first
    results = []
    for f in filings:
        print(f"{ticker}: analyzing {f['form']} filed {f['filed']}", file=sys.stderr)
        results.append(analyze_filing.analyze_filing(ticker, cik10, f))
    return results


def merge(state: dict, partials: dict[str, list[dict]]) -> None:
    tickers = all_tickers()
    fetch_fundamentals.update_fundamentals(state, tickers)
    for ticker, results in partials.items():
        for r in results:
            analyze_filing.record_result(state, ticker, r)

    seen = store.read_json(store.FILINGS_SEEN_FILE, {})
    for t in tickers:
        seen.pop(t, None)
    ciks, _ = check_filings.detect(tickers, seen)  # initializes seen-state from the live feeds
    for t in tickers:
        feed = edgar_client.recent_filings(edgar_client.get_submissions(ciks[t]), check_filings.WATCHED_FORMS)
        state["companies"][t]["recent_filings"] = [
            check_filings._event(ciks[t], f) for f in feed[: check_filings.EVENTS_KEPT]
        ]
    score.rescore_all(state)
    store.save_companies(state)
    store.write_json(store.FILINGS_SEEN_FILE, seen)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m screener.backfill")
    sub = parser.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("qualitative")
    q.add_argument("ticker")
    q.add_argument("--out", required=True, type=Path)
    m = sub.add_parser("merge")
    m.add_argument("partials", nargs="*", type=Path)
    sub.add_parser("all")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "qualitative":
            ticker = args.ticker.upper()
            store.write_json(args.out, {"ticker": ticker, "results": qualitative_for(ticker)})
            return 0

        if args.cmd == "merge":
            partials = {}
            for p in args.partials:
                data = json.loads(p.read_text())
                partials[data["ticker"]] = data["results"]
            missing = sorted(set(all_tickers()) - set(partials))
            if missing:
                print(f"::warning title=Backfill::No qualitative results for {', '.join(missing)}", file=sys.stderr)
        else:
            partials = {t: qualitative_for(t) for t in all_tickers()}

        merge(store.load_companies(), partials)
    except (edgar_client.EdgarError, fetch_fundamentals.FundamentalsError, analyze_filing.QualitativeError) as exc:
        print(f"ERROR: {exc}\nNothing written.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
