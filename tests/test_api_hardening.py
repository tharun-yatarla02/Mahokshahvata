import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from paper_trading.paper_trading_engine import PaperTradingEngine

AUTH = {"Authorization": "Bearer demo-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    monkeypatch.setattr(api_main, "engine", PaperTradingEngine(str(tmp_path / "paper_trading.db")))
    return TestClient(api_main.app)


@pytest.fixture
def portfolio_id(client):
    res = client.post("/portfolio", json={"name": "Test", "starting_capital": 1000}, headers=AUTH)
    assert res.status_code == 200
    return res.json()["portfolio_id"]


def test_add_cash_accepts_positive_amount(client, portfolio_id):
    res = client.post(f"/portfolio/{portfolio_id}/cash", json={"amount": 250}, headers=AUTH)

    assert res.status_code == 200


@pytest.mark.parametrize("body", [{"amount": "lots"}, {"amount": 0}, {"amount": -5}, {}])
def test_add_cash_rejects_invalid_amount(client, portfolio_id, body):
    res = client.post(f"/portfolio/{portfolio_id}/cash", json=body, headers=AUTH)

    assert res.status_code == 422


def test_politician_refresh_requires_auth(client, monkeypatch):
    monkeypatch.delenv("USE_DEMO_AUTH")
    monkeypatch.setattr(api_main, "refresh_cache", lambda: pytest.fail("refresh ran without auth"))

    res = client.post("/politicians/refresh")

    assert res.status_code == 401


def test_politician_refresh_runs_when_signed_in(client, monkeypatch):
    monkeypatch.setattr(api_main, "refresh_cache", lambda: 3)

    res = client.post("/politicians/refresh", headers=AUTH)

    assert res.status_code == 200
    assert res.json() == {"refreshed": 3}


def test_cors_rejects_unknown_origin_by_default(client):
    res = client.get("/health", headers={"Origin": "https://evil.example"})

    assert "access-control-allow-origin" not in res.headers


def test_cors_allows_local_dev_origin(client):
    res = client.get("/health", headers={"Origin": "http://localhost:5500"})

    assert res.headers["access-control-allow-origin"] == "http://localhost:5500"


def test_trade_executes_at_market_price_not_the_clients(client, portfolio_id, monkeypatch):
    monkeypatch.setattr(api_main, "latest_price", lambda ticker: 200.0)
    res = client.post(f"/portfolio/{portfolio_id}/trade-stock",
                      json={"ticker": "AAPL", "quantity": 1, "price": 0.01}, headers=AUTH)

    assert res.status_code == 200
    assert res.json()["price"] == 200.0
    assert res.json()["cash_balance"] == 800.0


def test_trade_stores_sector_and_uppercase_ticker(client, portfolio_id, monkeypatch):
    monkeypatch.setattr(api_main, "latest_price", lambda ticker: 200.0)
    res = client.post(f"/portfolio/{portfolio_id}/trade-stock",
                      json={"ticker": "aapl", "quantity": 1}, headers=AUTH)

    assert res.status_code == 200
    holding = client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()["holdings"][0]
    assert holding["ticker"] == "AAPL"
    assert holding["sector"] == "Technology"


def test_concurrent_buys_cannot_overspend(tmp_path):
    import threading
    engine = PaperTradingEngine(str(tmp_path / "race.db"))
    user = engine.get_or_create_user("u1")
    pid = engine.create_portfolio(user, "Race", 1000)

    def buy():
        try:
            engine.trade_stock(pid, user, "AAPL", 1, 100)
        except ValueError:
            pass  # insufficient cash is the expected outcome for the losers

    threads = [threading.Thread(target=buy) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert engine.get_portfolio_summary(pid, user)["cash_balance"] == 0


def test_news_sentiment_is_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "_sentiment_cache", {"data": None, "at": 0.0})
    monkeypatch.setattr(api_main, "collect_sentiment_results", lambda **kwargs: calls.append(1) or {"results": []})

    client.get("/news-sentiment")
    client.get("/news-sentiment")

    assert len(calls) == 1


@pytest.mark.parametrize("body", ['{"amount": Infinity}', '{"amount": NaN}', '{"amount": 1e12}'])
def test_add_cash_rejects_non_finite_or_huge_amounts(client, portfolio_id, body):
    res = client.post(f"/portfolio/{portfolio_id}/cash", content=body,
                      headers={**AUTH, "Content-Type": "application/json"})

    assert res.status_code == 422
    assert client.get(f"/portfolio/{portfolio_id}", headers=AUTH).status_code == 200


OPTION = {"ticker": "AAPL", "option_type": "CALL", "strike": 200, "expiry": "2099-12-18", "quantity": 1, "premium": 5}


@pytest.mark.parametrize("override", [{"ticker": ""}, {"ticker": "<img src=x>"}, {"expiry": "<img src=x onerror=alert(1)>"}, {"expiry": "soon"}])
def test_option_trade_rejects_bad_ticker_or_expiry(client, portfolio_id, override):
    res = client.post(f"/portfolio/{portfolio_id}/trade-option", json={**OPTION, **override}, headers=AUTH)

    assert res.status_code == 422


def test_option_trade_errors_use_400_not_404(client, portfolio_id):
    expired = client.post(f"/portfolio/{portfolio_id}/trade-option", json={**OPTION, "expiry": "2020-01-17"}, headers=AUTH)
    too_expensive = client.post(f"/portfolio/{portfolio_id}/trade-option", json={**OPTION, "premium": 999999}, headers=AUTH)
    missing = client.post("/portfolio/999999/trade-option", json=OPTION, headers=AUTH)

    assert expired.status_code == 400
    assert too_expensive.status_code == 400
    assert missing.status_code == 404


def test_portfolio_is_valued_at_the_tracked_price(client, portfolio_id, monkeypatch):
    tracked = {"BRK-B": {"current_price": 110.0, "sector": "Financials"}}
    monkeypatch.setattr(api_main.market_data, "lookup", lambda ticker: tracked.get(ticker))

    bought = client.post(f"/portfolio/{portfolio_id}/trade-stock",
                         json={"ticker": "brk.b", "quantity": 2}, headers=AUTH)
    tracked["BRK-B"] = {"current_price": 112.0, "sector": "Financials"}   # the market moves
    summary = client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()

    assert bought.status_code == 200 and bought.json()["price"] == 110.0
    holding = summary["holdings"][0]
    assert (holding["ticker"], holding["sector"], holding["current_price"]) == ("BRK-B", "Financials", 112.0)
    assert summary["total_pl"] == 4.0   # bought 2 at 110, now 112


@pytest.fixture
def market_at_192(monkeypatch):
    # AAPL tracked at $192.31; the portfolio fixture starts with $1,000.
    monkeypatch.setattr(api_main.market_data, "lookup",
                        lambda t: {"current_price": 192.31, "sector": "Technology"} if t == "AAPL" else None)


def trade(client, pid, path="trade-stock", **body):
    return client.post(f"/portfolio/{pid}/{path}", json={"ticker": "AAPL", **body}, headers=AUTH)


def test_buy_by_dollar_amount_gets_fractional_shares(client, portfolio_id, market_at_192):
    res = trade(client, portfolio_id, amount=100)

    assert res.status_code == 200
    assert res.json()["quantity"] == 0.519993        # $100 / $192.31, truncated to 6 decimals
    assert res.json()["value"] == 100.0
    assert res.json()["cash_balance"] == 900.0


def test_buying_more_than_cash_allows_explains_how_many_you_can_get(client, portfolio_id, market_at_192):
    preview = trade(client, portfolio_id, "trade-preview", quantity=10).json()
    res = trade(client, portfolio_id, quantity=10)

    assert preview["ok"] is False
    assert preview["max_buy_quantity"] == 5.199937 and preview["max_buy_whole_shares"] == 5
    assert "costs $1,923.10, you have $1,000.00. That buys up to 5.199937 shares" in preview["message"]
    assert res.status_code == 400 and res.json()["detail"] == preview["message"]
    assert client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()["cash_balance"] == 1000.0


def test_preview_does_not_trade_and_matches_the_trade(client, portfolio_id, market_at_192):
    preview = trade(client, portfolio_id, "trade-preview", quantity=2.5).json()
    assert client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()["holdings"] == []

    placed = trade(client, portfolio_id, quantity=2.5).json()

    assert preview["ok"] and preview["value"] == 480.78     # 2.5 x 192.31 = 480.775, rounded up for a buy
    assert (placed["quantity"], placed["value"], placed["cash_balance"]) == (2.5, 480.78, 519.22)


def test_minimum_order_and_selling_rules(client, portfolio_id, market_at_192):
    assert trade(client, portfolio_id, quantity=0.001).status_code == 400          # $0.19 order
    assert "You don't own any AAPL" in trade(client, portfolio_id, quantity=1, side="SELL").json()["detail"]

    trade(client, portfolio_id, amount=500)                                          # 2.599968 shares
    too_many = trade(client, portfolio_id, quantity=3, side="SELL")
    sell_all = trade(client, portfolio_id, quantity=2.599968, side="SELL")

    assert too_many.status_code == 400 and "You own 2.599968 AAPL" in too_many.json()["detail"]
    assert sell_all.status_code == 200
    summary = client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()
    assert summary["holdings"] == []
    assert summary["cash_balance"] == 999.99      # the buy rounds up and the sell rounds down: never free money


@pytest.mark.parametrize("body", [{}, {"quantity": 1, "amount": 100}, {"quantity": 0}, {"amount": "Infinity"}])
def test_order_needs_exactly_one_valid_size(client, portfolio_id, market_at_192, body):
    assert trade(client, portfolio_id, **body).status_code == 422


def test_default_portfolio_is_created_once(client):
    first = client.post("/portfolio/default", headers=AUTH).json()["portfolio_id"]
    again = client.post("/portfolio/default", headers=AUTH).json()["portfolio_id"]

    portfolios = client.get("/portfolio", headers=AUTH).json()["portfolios"]
    assert first == again
    assert [(p["id"], p["starting_capital"]) for p in portfolios] == [(first, 100000.0)]


def test_a_small_leftover_position_can_always_be_sold(client, portfolio_id, market_at_192, monkeypatch):
    trade(client, portfolio_id, amount=1.50)                                   # 0.0078 shares, worth $1.50
    monkeypatch.setattr(api_main.market_data, "lookup",                         # price drops: now worth ~$0.39
                        lambda t: {"current_price": 50.0, "sector": "Technology"} if t == "AAPL" else None)
    owned = client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()["holdings"][0]["quantity"]

    partial = trade(client, portfolio_id, quantity=owned / 2, side="SELL")
    everything = trade(client, portfolio_id, quantity=owned, side="SELL")

    assert partial.status_code == 400 and "Minimum order" in partial.json()["detail"]
    assert everything.status_code == 200
    assert client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()["holdings"] == []


def test_unknown_ticker_is_a_clear_404(client, portfolio_id, monkeypatch):
    monkeypatch.setattr(api_main, "latest_price", lambda ticker: None)
    res = client.post(f"/portfolio/{portfolio_id}/trade-preview", json={"ticker": "ZZZZ", "quantity": 1}, headers=AUTH)

    assert res.status_code == 404
    assert "Check the ticker" in res.json()["detail"]


def test_dotenv_loader_reads_keys_without_overriding_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nexport NEW_KEY="abc 123"\nEXISTING=from-file\n\nNOT_A_PAIR\n')
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.setenv("EXISTING", "from-environment")

    api_main._load_dotenv(env)

    assert api_main.os.environ["NEW_KEY"] == "abc 123"
    assert api_main.os.environ["EXISTING"] == "from-environment"
    monkeypatch.delenv("NEW_KEY")


def test_price_history_returns_momentum_window_and_caches(client, monkeypatch):
    import pandas as pd
    days = pd.bdate_range("2026-05-01", periods=100)
    calls = []

    def fake_history(tickers, lookback_days):
        calls.append(tickers)
        return pd.DataFrame({tickers[0]: [100.0 + i for i in range(100)]}, index=days)

    monkeypatch.setattr(api_main, "fetch_price_history", fake_history)
    monkeypatch.setattr(api_main, "_price_history_cache", {})

    points = client.get("/price-history/brk.b").json()["points"]

    assert calls == [["BRK-B"]]
    assert len(points) == 90                     # same window as momentum_pct
    assert points[0]["close"] == 110.0 and points[-1]["close"] == 199.0
    client.get("/price-history/BRK-B")
    assert len(calls) == 1                       # second request served from cache

    monkeypatch.setattr(api_main, "fetch_price_history", lambda tickers, lookback_days: pd.DataFrame())
    assert client.get("/price-history/ZZZZ").status_code == 404
