from momentum.momentum_engine import rank_momentum


def test_rank_momentum_uses_sector_lookup():
    ranked = rank_momentum(["AAPL", "MSFT", "NVDA"], lookback_days=30, include_benchmark=False)

    assert ranked
    sectors = {item["ticker"]: item["sector"] for item in ranked}
    assert sectors["AAPL"] != "Unknown"
    assert sectors["MSFT"] != "Unknown"
    assert sectors["NVDA"] != "Unknown"
