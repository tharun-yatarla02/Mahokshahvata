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

DEFAULT_SECTOR_LOOKUP = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "GOOGL": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "META": "Communication Services",
    "TSLA": "Automotive",
    "NVDA": "Technology",
    "AVGO": "Technology",
    "PLTR": "Technology",
    "CRWD": "Technology",
    "LLY": "Healthcare",
    "ISRG": "Healthcare",
    "XOM": "Energy",
    "CVX": "Energy",
    "JPM": "Financials",
    "BAC": "Financials",
    "WMT": "Consumer Staples",
    "COST": "Consumer Staples",
    "HD": "Consumer Discretionary",
    "DIS": "Communication Services",
}

# Short filter names the dashboard uses. A theme is a hand-picked ticker list
# that cuts across sectors; an alias is just a shorter name for one sector.
# Edit these to change what the AI / Growth / Tech / Health / Finance filters show.
THEME_TICKERS = {
    "ai": ["NVDA", "MSFT", "GOOGL", "META", "AVGO", "PLTR", "AMZN"],
    "growth": ["TSLA", "NVDA", "PLTR", "CRWD", "AMZN", "ISRG", "META"],
}

SECTOR_ALIASES = {
    "tech": "Technology",
    "health": "Healthcare",
    "finance": "Financials",
}


def filter_by_group(ranked, group):
    """Filters ranked momentum rows by a sector name, a sector alias
    ("Tech"), or a theme ("AI"). Matching is case-insensitive."""
    key = group.strip().lower()
    if key in THEME_TICKERS:
        tickers = set(THEME_TICKERS[key])
        return [item for item in ranked if item.get("ticker") in tickers]
    sector = SECTOR_ALIASES.get(key, group).lower()
    return [item for item in ranked if item.get("sector", "Unknown").lower() == sector]


def search_rows(rows, query):
    """Search by ticker, company name or sector, best matches first:
    exact ticker, then ticker prefix, then name prefix, then name/sector
    substring. Ties go to the larger company."""
    q = query.strip().lower()
    if not q:
        return list(rows)
    scored = []
    for row in rows:
        ticker = row.get("ticker", "").lower()
        name = (row.get("name") or "").lower()
        if ticker == q:
            score = 0
        elif ticker.startswith(q):
            score = 1
        elif name.startswith(q) or any(word.startswith(q) for word in name.split()):
            score = 2
        elif q in name or q in (row.get("sector") or "").lower():
            score = 3
        else:
            continue
        scored.append((score, -(row.get("market_cap") or 0), row))
    scored.sort(key=lambda item: item[:2])
    return [row for _, _, row in scored]


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


def latest_price(ticker):
    """Most recent close for one ticker, or None if it can't be fetched."""
    try:
        closes = fetch_price_history([ticker], lookback_days=5)
    except Exception:
        return None
    if closes.empty:
        return None
    series = closes.iloc[:, 0].dropna()
    return float(series.iloc[-1]) if not series.empty else None


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
    sector_lookup = sector_lookup or DEFAULT_SECTOR_LOOKUP
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
