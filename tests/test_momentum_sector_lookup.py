import pandas as pd

import momentum.momentum_engine as engine
from momentum.momentum_engine import filter_by_group, rank_momentum


def test_rank_momentum_uses_sector_lookup(monkeypatch):
    # Fixed prices instead of a live Yahoo download, so the test runs offline and can't flake.
    closes = pd.DataFrame({"AAPL": [100.0, 110.0], "MSFT": [200.0, 190.0], "NVDA": [50.0, 75.0]})
    monkeypatch.setattr(engine, "fetch_price_history", lambda tickers, lookback_days: closes[tickers])
    ranked = rank_momentum(["AAPL", "MSFT", "NVDA"], lookback_days=30, include_benchmark=False)

    assert [item["ticker"] for item in ranked] == ["NVDA", "AAPL", "MSFT"]   # +50%, +10%, -5%
    sectors = {item["ticker"]: item["sector"] for item in ranked}
    assert sectors["AAPL"] != "Unknown"
    assert sectors["MSFT"] != "Unknown"
    assert sectors["NVDA"] != "Unknown"


ROWS = [
    {"ticker": "NVDA", "sector": "Technology"},
    {"ticker": "AAPL", "sector": "Technology"},
    {"ticker": "LLY", "sector": "Healthcare"},
    {"ticker": "JPM", "sector": "Financials"},
    {"ticker": "XOM", "sector": "Energy"},
]


def test_filter_by_group_handles_themes_aliases_and_sectors():
    tickers = lambda group: [row["ticker"] for row in filter_by_group(ROWS, group)]

    assert tickers("AI") == ["NVDA"]
    assert tickers("Tech") == ["NVDA", "AAPL"]
    assert tickers("health") == ["LLY"]
    assert tickers("Finance") == ["JPM"]
    assert tickers("Energy") == ["XOM"]
    assert tickers("Nonexistent") == []


def test_ai_and_growth_are_rules_not_fixed_lists():
    rows = [
        {"ticker": "MU", "industry": "Semiconductors", "market_cap": 1.2e12, "momentum_pct": 5, "relative_strength": -1},
        {"ticker": "TINYCHIP", "industry": "Semiconductors", "market_cap": 2e9, "momentum_pct": 90, "relative_strength": 80},
        {"ticker": "AMZN", "industry": "Catalog/Specialty Distribution", "market_cap": 2.7e12, "momentum_pct": 3, "relative_strength": -2},
        {"ticker": "MRNA", "industry": "Biotechnology", "market_cap": 72e9, "momentum_pct": 278, "relative_strength": 274},
        {"ticker": "LAGGARD", "industry": "Banks", "market_cap": 50e9, "momentum_pct": 25, "relative_strength": -3},
    ]
    tickers = lambda group: [row["ticker"] for row in filter_by_group(rows, group)]

    assert tickers("AI") == ["MU", "AMZN"]          # industry + size, plus anchors
    assert tickers("Growth") == ["MRNA"]            # $10B+, up 20%+, beating the S&P 500
