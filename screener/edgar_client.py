"""Shared SEC EDGAR HTTP client.

Every pipeline script (fundamentals, filing watcher, MD&A text) goes through
this module so the User-Agent header, rate limiting, retry/backoff, and
fail-loud behavior are implemented once.

SEC's fair-access policy (https://www.sec.gov/os/webmaster-faq#developers)
requires a descriptive User-Agent with a real name/contact, and asks
automated tools not to hammer the service — we cap ourselves well under
their 10 req/sec limit and back off on any 4xx/5xx/timeout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_DIR = DATA_DIR / "state"
TICKER_CIK_CACHE = STATE_DIR / "cik_cache.json"

_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

_MIN_REQUEST_INTERVAL = 0.15  # seconds; keeps us well under SEC's 10 req/sec cap
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8, 16

_last_request_at = 0.0


class EdgarError(RuntimeError):
    """Raised when EDGAR can't be reached after retries, or returns something
    we can't use. Pipeline scripts let this propagate and exit non-zero —
    we never want a bad request silently turning into empty/partial data."""


def _user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise EdgarError(
            "EDGAR_USER_AGENT is not set. SEC requires a real name/contact "
            "in the User-Agent header (see .env.example)."
        )
    return ua


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _get(url: str, *, timeout: float = 20.0) -> requests.Response:
    headers = {"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"}
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _throttle()
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:  # network error, timeout, etc.
            last_exc = exc
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                last_exc = EdgarError(f"HTTP {resp.status_code} from {url}")
            else:
                # A 4xx that isn't rate-limiting (e.g. 404) won't be fixed by
                # retrying — fail immediately with a clear message.
                raise EdgarError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_BACKOFF_BASE * (2**attempt))
    raise EdgarError(f"Failed after {_MAX_RETRIES} attempts: {url}") from last_exc


def get_json(url: str) -> dict:
    resp = _get(url)
    try:
        return resp.json()
    except ValueError as exc:
        raise EdgarError(f"Non-JSON response from {url}") from exc


def get_text(url: str) -> str:
    return _get(url).text


def resolve_ciks(tickers: list[str], *, use_cache: bool = True) -> dict[str, str]:
    """Map ticker -> zero-padded 10-digit CIK string, verified against EDGAR's
    own ticker list rather than hardcoded. Raises EdgarError naming any
    ticker that isn't found, instead of silently dropping it."""
    if use_cache and TICKER_CIK_CACHE.exists():
        cached = json.loads(TICKER_CIK_CACHE.read_text())
        if all(t in cached for t in tickers):
            return {t: cached[t] for t in tickers}

    raw = get_json(_COMPANY_TICKERS_URL)
    by_ticker = {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in raw.values()}

    missing = [t for t in tickers if t not in by_ticker]
    if missing:
        raise EdgarError(
            f"Ticker(s) not found in EDGAR company_tickers.json: {missing}. "
            "Index membership may have changed — update screener/universe.py."
        )

    result = {t: by_ticker[t] for t in tickers}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TICKER_CIK_CACHE.write_text(json.dumps(result, indent=2, sort_keys=True))
    return result


def get_submissions(cik10: str) -> dict:
    return get_json(_SUBMISSIONS_URL.format(cik10=cik10))


def recent_filings(submissions: dict, forms: tuple[str, ...]) -> list[dict]:
    """Flatten the submissions feed's parallel arrays, newest first."""
    recent = submissions.get("filings", {}).get("recent", {})
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    items = recent.get("items") or []
    rows = [dict(zip(keys, vals)) for vals in zip(*(recent.get(k, []) for k in keys))]
    out = [
        {
            "accession": r["accessionNumber"],
            "form": r["form"],
            "filed": r["filingDate"],
            "period_end": r["reportDate"] or None,
            "primary_document": r["primaryDocument"],
            "items": items[i] if i < len(items) else "",
        }
        for i, r in enumerate(rows)
        if r["form"] in forms
    ]
    return sorted(out, key=lambda r: (r["filed"], r["accession"]), reverse=True)


def archive_url(cik10: str, accession: str, filename: str = "") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{accession.replace('-', '')}/{filename}"


def filing_documents(cik10: str, accession: str) -> list[str]:
    index = get_json(archive_url(cik10, accession, "index.json"))
    return [item["name"] for item in index.get("directory", {}).get("item", [])]


def get_companyfacts(cik10: str) -> dict:
    return get_json(_COMPANYFACTS_URL.format(cik10=cik10))
