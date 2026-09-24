"""Read/write the committed JSON data files.

Writes are atomic (temp file + rename) so a crash mid-write can never leave
a truncated file for the frontend to read.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from screener.universe import UNIVERSE

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COMPANIES_FILE = DATA_DIR / "companies.json"
PRICES_FILE = DATA_DIR / "prices.json"
FILINGS_SEEN_FILE = DATA_DIR / "state" / "filings_seen.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_companies() -> dict:
    """Return {"as_of": ..., "companies": {ticker: record}} keyed by ticker,
    with a skeleton record for every company in the universe."""
    raw = read_json(COMPANIES_FILE, {"as_of": None, "companies": []})
    by_ticker = {c["ticker"]: c for c in raw.get("companies", [])}
    for co in UNIVERSE:
        rec = by_ticker.setdefault(co.ticker, {"ticker": co.ticker})
        rec["name"] = co.name
        rec["sub_sector"] = co.sub_sector
    return {"as_of": raw.get("as_of"), "companies": by_ticker}


def save_companies(state: dict) -> None:
    order = [c.ticker for c in UNIVERSE]
    companies = [state["companies"][t] for t in order if t in state["companies"]]
    write_json(COMPANIES_FILE, {"as_of": utc_now_iso(), "companies": companies})
