from momentum import market_data
from momentum.market_data import MarketData
from momentum.momentum_engine import BENCHMARK_TICKER, search_rows


def screener_row(symbol, name, market_cap, price="$10.00", sector="Technology", pct="1.5%"):
    return {"symbol": symbol, "name": name, "marketCap": market_cap, "lastsale": price,
            "pctchange": pct, "volume": "1000", "sector": sector, "industry": ""}


class FakeResponse:
    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"rows": self.rows}}


def test_fetch_universe_keeps_largest_common_stocks(monkeypatch):
    rows = [
        screener_row("SMALL", "Small Co Common Stock", "1000000"),
        screener_row("AAPL", "Apple Inc. Common Stock", "4000000000000", "$337.02"),
        screener_row("BRK/B", "Berkshire Hathaway Inc.", "1000000000000", sector=""),
        screener_row("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.", "900000000000"),
        screener_row("ITUB", "Itau Unibanco American Depositary Shares (each representing 1 Preferred Share)", "50000000000", sector="Finance"),
        screener_row("ET", "Energy Transfer LP Common Units", "60000000000", sector="Energy"),
        # Not common stock: dropped even though they're large
        screener_row("ZIONP", "Zions Bancorporation Depositary Shares (Each representing 1/40th Interest)", "90000000000"),
        screener_row("NFEGP", "New Fortress Energy Inc. Series A Mandatorily Convertible Preferred Stock", "90000000000"),
        screener_row("CORZW", "Core Scientific Inc. Tranche 1 Warrants", "90000000000"),
        screener_row("ABR^D", "Arbor Realty 6.375% Preferred", "90000000000"),
        screener_row("NOCAP", "No Market Cap Inc. Common Stock", ""),
    ]
    monkeypatch.setattr(market_data.requests, "get", lambda *a, **k: FakeResponse(rows))

    universe = market_data.fetch_universe(limit=5)

    assert [s["ticker"] for s in universe] == ["AAPL", "BRK-B", "TSM", "ET", "ITUB"]
    apple = universe[0]
    assert apple["name"] == "Apple Inc."
    assert apple["current_price"] == 337.02
    assert apple["day_change_pct"] == 1.5
    assert universe[1]["sector"] == "Unknown"          # blank sector
    assert universe[4]["sector"] == "Financials"       # Nasdaq "Finance" renamed


def test_rebuild_ranks_by_momentum_from_live_price(tmp_path):
    data = MarketData(cache_path=tmp_path / "cache.json", universe_size=10)
    data._universe = [
        {"ticker": "UP", "name": "Up", "sector": "Technology", "market_cap": 1, "current_price": 150.0},
        {"ticker": "DOWN", "name": "Down", "sector": "Energy", "market_cap": 2, "current_price": 80.0},
        {"ticker": "NOHIST", "name": "New IPO", "sector": "Energy", "market_cap": 3, "current_price": 5.0},
    ]
    data._history = {
        "UP": {"start": 100.0, "last": 140.0},
        "DOWN": {"start": 100.0, "last": 85.0},
        BENCHMARK_TICKER: {"start": 100.0, "last": 110.0},
    }

    data._rebuild()

    rows = data.snapshot()
    assert [r["ticker"] for r in rows] == ["UP", "DOWN"]      # no history -> not ranked yet
    assert rows[0]["momentum_pct"] == 50.0                    # uses the live 150, not the 140 close
    assert rows[0]["relative_strength"] == 40.0               # vs. S&P +10%
    assert rows[0]["rank"] == 1
    assert data.lookup("down")["momentum_pct"] == -20.0
    assert data.sectors() == ["Energy", "Technology"]


def test_cache_round_trip(tmp_path):
    first = MarketData(cache_path=tmp_path / "cache.json")
    first._universe = [{"ticker": "UP", "name": "Up", "sector": "Technology", "market_cap": 1, "current_price": 150.0}]
    first._history = {"UP": {"start": 100.0, "last": 140.0}}
    first.quotes_updated_at = "2026-09-23T00:00:00+00:00"
    first._save()

    second = MarketData(cache_path=tmp_path / "cache.json")
    second._load()

    assert second.lookup("UP")["momentum_pct"] == 50.0
    assert second.quotes_updated_at == "2026-09-23T00:00:00+00:00"


def test_search_rows_ranks_ticker_matches_before_names():
    rows = [
        {"ticker": "APLE", "name": "Apple Hospitality REIT", "sector": "Real Estate", "market_cap": 3},
        {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology", "market_cap": 4000},
        {"ticker": "AP", "name": "Ampco-Pittsburgh", "sector": "Industrials", "market_cap": 1},
        {"ticker": "BAC", "name": "Bank of America", "sector": "Financials", "market_cap": 300},
    ]

    assert [r["ticker"] for r in search_rows(rows, "ap")] == ["AP", "APLE", "AAPL"]
    assert [r["ticker"] for r in search_rows(rows, "apple")] == ["AAPL", "APLE"]
    assert [r["ticker"] for r in search_rows(rows, "america")] == ["BAC"]
    assert search_rows(rows, "zzz") == []
