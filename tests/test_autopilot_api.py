"""Autopilot endpoints: settings, preview, a real run, the run log, and the
rule that it never touches the user's own holdings."""
import pytest
from fastapi.testclient import TestClient

import api.main as api_main

DEMO = {"Authorization": "Bearer demo-token"}


class FakeMarket:
    def __init__(self, rows):
        self.rows = {r["ticker"]: r for r in rows}

    def status(self):
        return {"tracked": len(self.rows), "warming_up": not self.rows, "stale": False}

    def snapshot(self):
        return list(self.rows.values())

    def lookup(self, ticker):
        return self.rows.get((ticker or "").upper())


OPEN = {"open": True, "reason": None, "closes_at": "2026-10-12T16:00:00-04:00", "next_open": None}
SATURDAY = {"open": False, "reason": "Saturday", "closes_at": None, "next_open": "2026-10-12T09:30:00-04:00"}


def stock(ticker, price, momentum, strength, cap=1e11):
    return {"ticker": ticker, "current_price": price, "momentum_pct": momentum, "relative_strength": strength,
            "market_cap": cap, "sector": "Technology"}


@pytest.fixture
def setup(make_engine, monkeypatch):
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    monkeypatch.setattr(api_main, "engine", make_engine())
    market = FakeMarket([stock("AAPL", 200, 20, 10), stock("NVDA", 100, 40, 30), stock("XOM", 50, -5, -15)])
    monkeypatch.setattr(api_main, "market_data", market)
    monkeypatch.setattr(api_main, "_autopilot_news", lambda rows: {})
    monkeypatch.setattr(api_main, "market_status", lambda now=None: OPEN)   # tests must not depend on today's date
    client = TestClient(api_main.app)
    portfolio_id = client.post("/portfolio/default", headers=DEMO).json()["portfolio_id"]
    return client, market, portfolio_id


def test_settings_start_off_and_can_be_changed_within_limits(setup):
    client, _, pid = setup
    view = client.get(f"/portfolio/{pid}/autopilot", headers=DEMO).json()
    assert view["enabled"] is False and view["settings"]["max_positions"] == 8 and view["runs"] == []

    view = client.put(f"/portfolio/{pid}/autopilot", json={"enabled": True, "max_positions": 2}, headers=DEMO).json()
    assert view["enabled"] is True and view["settings"]["max_positions"] == 2
    assert client.put(f"/portfolio/{pid}/autopilot", json={"max_positions": 99}, headers=DEMO).status_code == 422


def test_preview_places_nothing_and_run_trades_and_logs(setup):
    client, _, pid = setup
    plan = client.post(f"/portfolio/{pid}/autopilot/preview", headers=DEMO).json()
    assert [(t["side"], t["ticker"]) for t in plan["trades"]] == [("BUY", "NVDA"), ("BUY", "AAPL")]
    assert client.get(f"/portfolio/{pid}", headers=DEMO).json()["holdings"] == []      # preview bought nothing

    run = client.post(f"/portfolio/{pid}/autopilot/run", headers=DEMO).json()
    assert all(t["result"] == "done" for t in run["trades"])
    held = {h["ticker"] for h in client.get(f"/portfolio/{pid}", headers=DEMO).json()["holdings"]}
    assert held == {"NVDA", "AAPL"}
    history = client.get(f"/portfolio/{pid}/trade-history", headers=DEMO).json()["trades"]
    assert {t.get("source") for t in history} == {"autopilot"}
    view = client.get(f"/portfolio/{pid}/autopilot", headers=DEMO).json()
    assert set(view["managed"]) == {"NVDA", "AAPL"} and view["runs"][0]["trigger"] == "manual"


def test_never_sells_the_users_own_holdings(setup):
    client, market, pid = setup
    client.post(f"/portfolio/{pid}/trade-stock", json={"ticker": "XOM", "side": "BUY", "amount": 500}, headers=DEMO)
    plan = client.post(f"/portfolio/{pid}/autopilot/preview", headers=DEMO).json()
    assert "XOM" not in {t["ticker"] for t in plan["trades"] if t["side"] == "SELL"}   # falling, but it's the user's


def test_sells_its_own_position_when_the_trend_breaks(setup):
    client, market, pid = setup
    client.put(f"/portfolio/{pid}/autopilot", json={"max_positions": 1}, headers=DEMO)
    client.post(f"/portfolio/{pid}/autopilot/run", headers=DEMO)                 # buys NVDA
    market.rows["NVDA"] = stock("NVDA", 95, -3, -20)
    run = client.post(f"/portfolio/{pid}/autopilot/run", headers=DEMO).json()
    assert ("SELL", "NVDA") in [(t["side"], t["ticker"]) for t in run["trades"]]
    assert "NVDA" not in client.get(f"/portfolio/{pid}/autopilot", headers=DEMO).json()["managed"]


def test_stale_prices_mean_no_trades(setup, monkeypatch):
    client, market, pid = setup
    monkeypatch.setattr(market, "status", lambda: {"tracked": 3, "warming_up": False, "stale": True})
    plan = client.post(f"/portfolio/{pid}/autopilot/run", headers=DEMO).json()
    assert plan["trades"] == [] and "out of date" in plan["notes"][0]


def test_other_users_cannot_touch_the_autopilot(setup):
    client, _, pid = setup
    api_main.app.dependency_overrides[api_main.get_current_user] = lambda: 999
    try:
        assert client.get(f"/portfolio/{pid}/autopilot", headers=DEMO).status_code == 403
        assert client.post(f"/portfolio/{pid}/autopilot/run", headers=DEMO).status_code == 403
    finally:
        api_main.app.dependency_overrides.clear()


def test_no_trades_while_the_market_is_closed(setup, monkeypatch):
    client, _, pid = setup
    monkeypatch.setattr(api_main, "market_status", lambda now=None: SATURDAY)
    run = client.post(f"/portfolio/{pid}/autopilot/run", headers=DEMO).json()
    assert run["status"] == "closed" and run["trades"] == []
    assert "Market closed (Saturday)" in run["notes"][0] and "Mon Oct 12, 9:30" in run["notes"][0]
    assert client.get(f"/portfolio/{pid}", headers=DEMO).json()["holdings"] == []
    preview = client.post(f"/portfolio/{pid}/autopilot/preview", headers=DEMO).json()
    assert preview["trades"] and "last closing prices" in preview["notes"][0]   # preview still allowed, labelled
    view = client.get(f"/portfolio/{pid}/autopilot", headers=DEMO).json()
    assert view["market"]["open"] is False and view["runs"] == []

