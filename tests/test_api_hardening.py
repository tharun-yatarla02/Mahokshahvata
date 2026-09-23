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
