"""Share-price history for the demo companies, for the chart on the match screen.

Source: Yahoo Finance's public chart endpoint (delayed prices). The prototype stores a snapshot in
prices.json, which the page embeds; in the live app prices come from the broker partner's market data.

Usage:
  python fetch_prices.py            # writes prices.json next to this script
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
TICKERS = ["NVDA", "AMD", "TSM", "AAPL", "MSFT", "GOOGL", "AMZN", "WMT", "TSLA", "KO", "JPM", "LLY"]
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (equiteas-prototype)"}


def series(ticker: str, rng: str, interval: str) -> tuple[list, dict]:
    r = requests.get(URL.format(ticker=ticker), params={"range": rng, "interval": interval}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    points = [[ts, round(c, 2)] for ts, c in zip(result["timestamp"], closes) if c is not None]
    return points, result["meta"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    out = {"source": "Yahoo Finance (delayed)", "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "prices": {}}
    for ticker in TICKERS:
        daily, meta = series(ticker, "1y", "1d")
        weekly, _ = series(ticker, "5y", "1wk")
        out["prices"][ticker] = {"currency": meta.get("currency", "USD"), "exchange": meta.get("fullExchangeName"),
                                 "daily": daily, "weekly": weekly}
        print(f"{ticker}: {len(daily)} daily, {len(weekly)} weekly, last close {daily[-1][1]} {meta.get('currency')}")
        time.sleep(0.5)
    (HERE / "prices.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote prices.json ({(HERE / 'prices.json').stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
