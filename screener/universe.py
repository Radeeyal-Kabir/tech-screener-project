"""The tracked company universe.

This is a proposed set of ~20 S&P 500 Information Technology names, spread
across sub-sectors so the screener isn't just another semiconductor list.
Swap tickers in/out here as needed.

CIKs are deliberately NOT hardcoded: index membership and CIKs can change,
so ``edgar_client.resolve_ciks()`` looks each ticker up against SEC EDGAR's
``company_tickers.json`` at run time and caches the result. If a ticker in
this list is missing from that feed, every pipeline script fails loudly
rather than silently skipping it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    sub_sector: str


UNIVERSE: list[Company] = [
    Company("NVDA", "Nvidia", "Semiconductors"),
    Company("AVGO", "Broadcom", "Semiconductors"),
    Company("AMD", "Advanced Micro Devices", "Semiconductors"),
    Company("QCOM", "Qualcomm", "Semiconductors"),
    Company("TXN", "Texas Instruments", "Semiconductors"),
    Company("AMAT", "Applied Materials", "Semiconductor Equipment"),
    Company("LRCX", "Lam Research", "Semiconductor Equipment"),
    Company("MU", "Micron Technology", "Semiconductors (Memory)"),
    Company("MSFT", "Microsoft", "Systems Software"),
    Company("ORCL", "Oracle", "Systems Software"),
    Company("CRM", "Salesforce", "Application Software"),
    Company("ADBE", "Adobe", "Application Software"),
    Company("INTU", "Intuit", "Application Software"),
    Company("NOW", "ServiceNow", "Application Software"),
    Company("PANW", "Palo Alto Networks", "Systems Software (Security)"),
    Company("AAPL", "Apple", "Technology Hardware"),
    Company("CSCO", "Cisco Systems", "Communications Equipment"),
    Company("IBM", "IBM", "IT Services"),
    Company("ACN", "Accenture", "IT Services"),
    Company("HPE", "Hewlett Packard Enterprise", "Technology Hardware"),
]


def tickers() -> list[str]:
    return [c.ticker for c in UNIVERSE]


def by_ticker(ticker: str) -> Company:
    for c in UNIVERSE:
        if c.ticker == ticker:
            return c
    raise KeyError(f"{ticker!r} is not in the tracked universe")
