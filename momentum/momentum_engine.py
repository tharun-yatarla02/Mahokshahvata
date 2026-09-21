"""
momentum_engine.py

Computes momentum rankings for a list of tickers using free, no-API-key
historical price data (yfinance). Momentum here is defined as trailing
% price return over a lookback window, optionally adjusted for relative
strength against a benchmark index (S&P 500 by default).

Setup:
    pip install yfinance pandas

Usage as a library:
    from momentum_engine import rank_momentum

    tickers = ["NVDA", "PLTR", "AVGO", "XOM", "JPM"]
    ranked = rank_momentum(tickers, lookback_days=90)
    # -> sorted list of dicts: ticker, sector, current_price, momentum_pct, relative_strength

Run directly for a quick console report:
    python momentum_engine.py

Extend later:
    - Swap yfinance for Finnhub/Polygon for production-grade, rate-limit-friendly data
    - Add multiple lookback windows (30/90/180 day) and blend them into one score
    - Persist rankings to your database on a schedule instead of printing
"""

import sys
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

BENCHMARK_TICKER = "^GSPC"  # S&P 500, used for relative strength

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "AVGO",
    "PLTR", "CRWD", "LLY", "ISRG", "XOM", "CVX", "JPM", "BAC",
    "WMT", "COST", "HD", "DIS",
]


def fetch_price_history(tickers, lookback_days=90):
    """Returns a DataFrame of adjusted close prices, tickers as columns."""
    period = f"{lookback_days + 10}d"  # small buffer for weekends/holidays
    data = yf.download(tickers, period=period, progress=False, auto_adjust=True)
    if data.empty:
        return pd.DataFrame()
    # yfinance returns a MultiIndex when multiple tickers are requested
    if isinstance(data.columns, pd.MultiIndex):
        closes = data["Close"]
    else:
        closes = data[["Close"]]
        closes.columns = tickers
    return closes.dropna(how="all")


def compute_momentum(closes, lookback_days=90):
    """Given a price DataFrame, compute % return over the lookback window per ticker."""
    momentum = {}
    for ticker in closes.columns:
        series = closes[ticker].dropna()
        if len(series) < 2:
            continue
        start_price = series.iloc[max(0, len(series) - lookback_days)]
        end_price = series.iloc[-1]
        if start_price <= 0:
            continue
        pct_change = ((end_price - start_price) / start_price) * 100
        momentum[ticker] = {
            "current_price": round(float(end_price), 2),
            "momentum_pct": round(float(pct_change), 2),
        }
    return momentum


def rank_momentum(tickers, sector_lookup=None, lookback_days=90, include_benchmark=True):
    """
    tickers: list of ticker strings
    sector_lookup: optional dict {ticker: sector} to attach sector labels
    lookback_days: trailing window for the momentum calculation
    include_benchmark: also compute relative strength vs the S&P 500

    Returns a list of dicts, ranked best momentum first.
    """
    sector_lookup = sector_lookup or {}
    all_tickers = list(tickers)
    if include_benchmark:
        all_tickers = all_tickers + [BENCHMARK_TICKER]

    closes = fetch_price_history(all_tickers, lookback_days)
    if closes.empty:
        return []

    momentum = compute_momentum(closes, lookback_days)

    benchmark_pct = momentum.get(BENCHMARK_TICKER, {}).get("momentum_pct")

    ranked = []
    for ticker in tickers:
        m = momentum.get(ticker)
        if m is None:
            continue
        entry = {
            "ticker": ticker,
            "sector": sector_lookup.get(ticker, "Unknown"),
            "current_price": m["current_price"],
            "momentum_pct": m["momentum_pct"],
        }
        if benchmark_pct is not None:
            entry["relative_strength"] = round(m["momentum_pct"] - benchmark_pct, 2)
        ranked.append(entry)

    ranked.sort(key=lambda x: x["momentum_pct"], reverse=True)
    for i, entry in enumerate(ranked, start=1):
        entry["rank"] = i

    return ranked


def print_report(ranked, top_n=10, lookback_days=90):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*80}\nMOMENTUM RANKINGS ({lookback_days}-day) — {stamp}\n{'='*80}\n")
    if not ranked:
        print("No data returned — check your network connection and ticker list.")
        return
    print(f"{'Rank':<6}{'Ticker':<8}{'Sector':<16}{'Price':<12}{'Momentum':<12}{'Rel. Strength':<14}")
    for entry in ranked[:top_n]:
        rs = entry.get("relative_strength", "n/a")
        print(f"{entry['rank']:<6}{entry['ticker']:<8}{entry['sector']:<16}"
              f"${entry['current_price']:<11}{entry['momentum_pct']:+.2f}%      {rs}")


if __name__ == "__main__":
    tickers = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_UNIVERSE
    ranked = rank_momentum(tickers, lookback_days=90)
    print_report(ranked, top_n=10)
