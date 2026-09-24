from datetime import date, timedelta

import pytest

from screener import fetch_prices as fp
from screener import store

END = date(2026, 9, 23)


def _bars(n=260, start=100.0, step=0.5, end=END):
    return [(end - timedelta(days=n - 1 - i), start + step * i) for i in range(n)]


def test_parse_stooq_csv():
    text = "Date,Open,High,Low,Close,Volume\n2026-09-22,1,1,1,10.5,100\n2026-09-23,1,1,1,11.0,100\n"
    assert fp.parse_stooq_csv(text) == [(date(2026, 9, 22), 10.5), (date(2026, 9, 23), 11.0)]


@pytest.mark.parametrize("text", ["Exceeded the daily hits limit", "", "<html>blocked</html>"])
def test_non_csv_reply_is_an_error(text):
    with pytest.raises(fp.PriceError):
        fp.parse_stooq_csv(text)


def test_ticker_metrics():
    m = fp.ticker_metrics(_bars())
    closes = [c for _, c in _bars()]
    assert m["close"] == closes[-1]
    assert m["change_1d"] == round(closes[-1] / closes[-2] - 1, 4)
    assert m["sma200"] == round(sum(closes[-200:]) / 200, 2)
    assert m["vs_sma50"] > 0  # steadily rising series sits above its moving average
    assert len(m["closes"]) == fp.CLOSES_KEPT


def test_short_history_leaves_sma200_blank():
    m = fp.ticker_metrics(_bars(n=60))
    assert m["sma200"] is None and m["vs_sma200"] is None and m["sma50"] is not None


def test_build_prices_movers_and_relative_performance():
    histories = {
        "UP": (_bars(step=1.0), "stooq"),
        "FLAT": (_bars(step=0.0), "stooq"),
        "DOWN": (_bars(start=300, step=-0.5), "stooq"),
    }
    data = fp.build_prices(histories)
    assert data["movers"]["day"]["winners"][0] == "UP"
    assert data["movers"]["day"]["losers"][0] == "DOWN"
    rel = {r["ticker"]: r["rel_universe_3m"] for r in data["prices"]}
    assert rel["UP"] > 0 > rel["DOWN"]


def test_stale_ticker_fails():
    histories = {"A": (_bars(), "stooq"), "B": (_bars(end=END - timedelta(days=10)), "stooq")}
    with pytest.raises(fp.PriceError, match="Stale"):
        fp.build_prices(histories)


def test_falls_back_to_yfinance(monkeypatch):
    def bad_stooq(t, today):
        raise fp.PriceError("limit")

    monkeypatch.setattr(fp, "fetch_stooq", bad_stooq)
    monkeypatch.setattr(fp, "fetch_yfinance", lambda t, today: _bars())
    bars, source = fp.fetch_history("AAPL", END)
    assert source == "yfinance" and bars == _bars()


def test_main_writes_nothing_when_a_ticker_fails(monkeypatch, tmp_path):
    out = tmp_path / "prices.json"
    out.write_text('{"sentinel": true}')
    monkeypatch.setattr(store, "PRICES_FILE", out)

    def fetch(t, today):
        if t == "MSFT":
            raise fp.PriceError("MSFT: both sources failed")
        return _bars(), "stooq"

    monkeypatch.setattr(fp, "fetch_history", fetch)
    assert fp.main([]) == 1
    assert out.read_text() == '{"sentinel": true}'


def test_main_writes_on_success(monkeypatch, tmp_path):
    out = tmp_path / "prices.json"
    monkeypatch.setattr(store, "PRICES_FILE", out)
    monkeypatch.setattr(fp, "fetch_history", lambda t, today: (_bars(), "stooq"))
    assert fp.main([]) == 0
    assert '"market_date"' in out.read_text()
