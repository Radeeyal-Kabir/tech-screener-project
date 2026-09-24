"""Price pipeline: daily OHLCV -> % changes, movers, and momentum metrics.

Usage:
    python -m screener.fetch_prices

Source: Stooq's free CSV endpoint, with yfinance as a per-ticker fallback.
Stooq can answer HTTP 200 with a plain-text message instead of data (e.g.
a daily-limit notice), so a response only counts if it parses as a CSV
with recent rows.

Momentum (position vs. 50/200-day moving average, 3-month return relative
to the universe average) is computed here and displayed on its own — it is
not an input to the composite score (see score.py).

All-or-nothing: if any ticker can't be fetched from either source, or comes
back stale, nothing is written and the process exits non-zero.
"""

from __future__ import annotations

import csv
import io
import sys
import time
from datetime import date, timedelta

import requests

from screener import store
from screener.universe import tickers as all_tickers

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}.us&i=d&d1={d1}&d2={d2}"
HISTORY_DAYS = 420  # calendar days; comfortably > 200 trading days for the 200-day MA
CLOSES_KEPT = 60  # for sparklines on the dashboard
MOVERS_SHOWN = 5
MAX_STALE_DAYS = 4  # a ticker's last bar may trail the freshest ticker by this much (weekends/holidays)
THREE_MONTHS = 63  # trading days

_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0

Bar = tuple[date, float]  # (date, close)


class PriceError(RuntimeError):
    pass


def _http_get(url: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "tech-screener/1.0"})
            if resp.status_code == 200:
                return resp.text
            last_exc = PriceError(f"HTTP {resp.status_code} from {url}")
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_BACKOFF_BASE * (2**attempt))
    raise PriceError(f"Failed after {_MAX_RETRIES} attempts: {url}") from last_exc


def parse_stooq_csv(text: str) -> list[Bar]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Close" not in reader.fieldnames or "Date" not in reader.fieldnames:
        raise PriceError(f"Stooq returned non-CSV content: {text[:120]!r}")
    bars = []
    for row in reader:
        try:
            bars.append((date.fromisoformat(row["Date"]), float(row["Close"])))
        except (ValueError, TypeError):
            continue
    if not bars:
        raise PriceError("Stooq CSV had no rows")
    return sorted(bars)


def fetch_stooq(ticker: str, today: date) -> list[Bar]:
    url = STOOQ_URL.format(
        symbol=ticker.lower().replace(".", "-"),
        d1=(today - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d"),
        d2=today.strftime("%Y%m%d"),
    )
    return parse_stooq_csv(_http_get(url))


def fetch_yfinance(ticker: str, today: date) -> list[Bar]:
    import yfinance as yf

    df = yf.download(
        ticker,
        start=(today - timedelta(days=HISTORY_DAYS)).isoformat(),
        end=(today + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=True,
        progress=False,
    )
    if df is None or df.empty:
        raise PriceError(f"yfinance returned no data for {ticker}")
    close = df["Close"]
    if hasattr(close, "columns"):  # newer yfinance returns a per-ticker column frame
        close = close.iloc[:, 0]
    return sorted((idx.date(), float(v)) for idx, v in close.dropna().items())


def fetch_history(ticker: str, today: date) -> tuple[list[Bar], str]:
    try:
        return fetch_stooq(ticker, today), "stooq"
    except PriceError as stooq_exc:
        print(f"{ticker}: Stooq failed ({stooq_exc}); trying yfinance", file=sys.stderr)
        try:
            return fetch_yfinance(ticker, today), "yfinance"
        except Exception as yf_exc:
            raise PriceError(f"{ticker}: both sources failed — stooq: {stooq_exc}; yfinance: {yf_exc}") from yf_exc


def _pct(new: float, old: float | None) -> float | None:
    return None if not old else round(new / old - 1, 4)


def _sma(closes: list[float], n: int) -> float | None:
    return None if len(closes) < n else sum(closes[-n:]) / n


def ticker_metrics(bars: list[Bar]) -> dict:
    closes = [c for _, c in bars]
    last_date, last = bars[-1]
    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)
    return {
        "date": last_date.isoformat(),
        "close": round(last, 2),
        "change_1d": _pct(last, closes[-2] if len(closes) >= 2 else None),
        "change_5d": _pct(last, closes[-6] if len(closes) >= 6 else None),
        "return_3m": _pct(last, closes[-(THREE_MONTHS + 1)] if len(closes) > THREE_MONTHS else None),
        "sma50": None if sma50 is None else round(sma50, 2),
        "sma200": None if sma200 is None else round(sma200, 2),
        "vs_sma50": _pct(last, sma50),
        "vs_sma200": _pct(last, sma200),
        "closes": [[d.isoformat(), round(c, 2)] for d, c in bars[-CLOSES_KEPT:]],
    }


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def build_prices(histories: dict[str, tuple[list[Bar], str]]) -> dict:
    rows = []
    for ticker, (bars, source) in histories.items():
        rows.append({"ticker": ticker, "source": source, **ticker_metrics(bars)})

    freshest = max(date.fromisoformat(r["date"]) for r in rows)
    stale = [r["ticker"] for r in rows if (freshest - date.fromisoformat(r["date"])).days > MAX_STALE_DAYS]
    if stale:
        raise PriceError(f"Stale price data (last bar > {MAX_STALE_DAYS} days behind {freshest}): {stale}")

    avg = {k: _mean([r[k] for r in rows]) for k in ("change_1d", "change_5d", "return_3m")}
    for r in rows:
        r["rel_universe_3m"] = (
            None if r["return_3m"] is None or avg["return_3m"] is None
            else round(r["return_3m"] - avg["return_3m"], 4)
        )

    def movers(key: str) -> dict:
        ranked = sorted((r for r in rows if r[key] is not None), key=lambda r: r[key], reverse=True)
        return {
            "winners": [r["ticker"] for r in ranked[:MOVERS_SHOWN]],
            "losers": [r["ticker"] for r in reversed(ranked[-MOVERS_SHOWN:])],
        }

    return {
        "as_of": store.utc_now_iso(),
        "market_date": freshest.isoformat(),
        "universe_avg": avg,
        "movers": {"day": movers("change_1d"), "week": movers("change_5d")},
        "prices": rows,
    }


def main(argv: list[str]) -> int:
    today = date.today()
    histories: dict[str, tuple[list[Bar], str]] = {}
    try:
        for t in all_tickers():
            histories[t] = fetch_history(t, today)
        data = build_prices(histories)
    except PriceError as exc:
        print(f"ERROR: {exc}\nNothing written.", file=sys.stderr)
        return 1
    store.write_json(store.PRICES_FILE, data)
    print(f"Wrote {len(data['prices'])} tickers for {data['market_date']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
