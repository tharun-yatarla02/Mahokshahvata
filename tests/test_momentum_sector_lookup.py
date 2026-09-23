from momentum.momentum_engine import filter_by_group, rank_momentum


def test_rank_momentum_uses_sector_lookup():
    ranked = rank_momentum(["AAPL", "MSFT", "NVDA"], lookback_days=30, include_benchmark=False)

    assert ranked
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
