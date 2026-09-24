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


def test_trade_rejects_price_far_from_market(client, portfolio_id, monkeypatch):
    monkeypatch.setattr(api_main, "latest_price", lambda ticker: 200.0)
    res = client.post(f"/portfolio/{portfolio_id}/trade-stock",
                      json={"ticker": "AAPL", "quantity": 1, "price": 0.01}, headers=AUTH)

    assert res.status_code == 400


def test_trade_near_market_price_stores_sector_and_uppercase_ticker(client, portfolio_id, monkeypatch):
    monkeypatch.setattr(api_main, "latest_price", lambda ticker: 200.0)
    res = client.post(f"/portfolio/{portfolio_id}/trade-stock",
                      json={"ticker": "aapl", "quantity": 1, "price": 202}, headers=AUTH)

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
    monkeypatch.setattr(api_main, "collect_sentiment_results", lambda: calls.append(1) or {"results": []})

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
                         json={"ticker": "brk.b", "quantity": 2, "price": 108}, headers=AUTH)
    summary = client.get(f"/portfolio/{portfolio_id}", headers=AUTH).json()

    assert bought.status_code == 200
    holding = summary["holdings"][0]
    assert (holding["ticker"], holding["sector"], holding["current_price"]) == ("BRK-B", "Financials", 110.0)
    assert summary["total_pl"] == 4.0   # bought 2 at 108, now 110
