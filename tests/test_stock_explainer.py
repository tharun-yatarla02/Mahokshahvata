"""'Explain this stock': the facts it is built from, the rule-based text,
the Claude path, and the /explain endpoint."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from explain.stock_explainer import (
    DISCLAIMER, StockExplanation, build_facts, explain_with_claude, explain_with_rules,
)

MARKET = [
    {"ticker": "NVDA", "name": "NVIDIA Corporation", "sector": "Technology", "market_cap": 5e12,
     "current_price": 225.07, "day_change_pct": 2.4, "momentum_pct": 18.0, "relative_strength": 11.5},
    {"ticker": "MSFT", "name": "Microsoft Corporation", "sector": "Technology", "market_cap": 3e12,
     "current_price": 500.0, "day_change_pct": 1.0, "momentum_pct": 5.0},
    {"ticker": "INTC", "name": "Intel Corporation", "sector": "Technology", "market_cap": 1e11,
     "current_price": 30.0, "day_change_pct": -1.0, "momentum_pct": -8.0},
    {"ticker": "TINY", "name": "Tiny Energy Co", "sector": "Energy", "market_cap": 5e8,
     "current_price": 4.0, "day_change_pct": -7.5, "momentum_pct": -20.0, "relative_strength": -27.0},
]
NEWS = [
    {"title": "Nvidia beats estimates", "tickers": "NVDA", "sentiment": "Positive", "source": "Wire",
     "published_ts": "2026-09-30T10:00:00+00:00", "reasoning": "beat"},
    {"title": "Nvidia faces export probe", "tickers": "NVDA, AMD", "sentiment": "Negative", "source": "Wire",
     "published_ts": "2026-09-30T12:00:00+00:00", "reasoning": "probe"},
    {"title": "Tariffs hit chipmakers", "tickers": "", "sectors_affected": "Technology, Industrials",
     "themes": "tariff", "sentiment": "Negative", "published_ts": "2026-09-29T09:00:00+00:00"},
    {"title": "Oil slides", "tickers": "", "sectors_affected": "Energy", "sentiment": "Negative"},
    {"title": "Unparsed", "tickers": "NVDA", "sentiment": "Unknown"},
]
FUNDAMENTALS = {
    "annual": {
        "revenue": [{"value": 130e9, "unit": "USD", "period_end": "2026-01-25"},
                    {"value": 61e9, "unit": "USD", "period_end": "2025-01-26"}],
        "net_income": [{"value": 72e9, "unit": "USD", "period_end": "2026-01-25"}],
    },
    "latest": {"long_term_debt": {"value": 8.5e9, "unit": "USD", "period_end": "2026-07-27"}},
}


def test_facts_pair_company_and_sector_news_with_the_sector_comparison():
    facts = build_facts(MARKET[0], MARKET, NEWS, FUNDAMENTALS)

    news = facts["news"]
    assert (news["company_mentions"], news["positive"], news["negative"]) == (2, 1, 1)
    assert news["company_headlines"][0]["title"] == "Nvidia faces export probe"  # newest first
    assert [a["title"] for a in news["sector_headlines"]] == ["Tariffs hit chipmakers"]
    assert facts["sector_comparison"] == {
        "stocks_in_sector": 3, "rank_by_90d_trend": 1, "sector_median_90d_pct": 5.0, "sector_median_day_pct": 1.0,
    }
    assert facts["fundamentals"]["revenue"]["change_pct"] == 113.1
    assert "change_pct" not in facts["fundamentals"]["net_income"]  # only one year reported


def test_rule_explanation_covers_every_section():
    e = explain_with_rules(build_facts(MARKET[0], MARKET, NEWS, FUNDAMENTALS))

    assert e.summary.startswith("NVIDIA Corporation (NVDA) trades at $225.07, up 2.40% today")
    assert any("1 positive and 1 negative of 2" in p for p in e.why_moving)
    assert any("tariff" in p for p in e.why_moving)
    assert any("Ahead of the S&P 500 by 11.5" in p for p in e.trend)
    assert any("#1 of 3 tracked Technology stocks" in p for p in e.trend)
    assert any(p.startswith("Revenue: $130.0B") and "+113.1%" in p for p in e.financials)
    assert e.bottom_line.endswith(DISCLAIMER)


def test_rule_explanation_flags_risks_and_missing_data():
    facts = build_facts(MARKET[3], MARKET, NEWS, None, keyword_tone=True)
    e = explain_with_rules(facts)

    assert e.financials == ["No SEC financial statements are available for this company."]
    assert any("90-day trend is down" in r for r in e.risks)
    assert any("7.5% move in one day" in r for r in e.risks)
    assert any("smaller company" in r for r in e.risks)
    assert "sector_comparison" not in facts  # the only Energy stock: nothing to compare with


def fake_client(parsed, stop_reason="end_turn"):
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse))), calls


def test_claude_explanation_gets_the_facts_and_keeps_the_disclaimer():
    written = StockExplanation(summary="s", why_moving=["w"], trend=["t"], financials=["f"], risks=["r"],
                               bottom_line="Strong run.")
    client, calls = fake_client(written)
    facts = build_facts(MARKET[0], MARKET, NEWS, FUNDAMENTALS)

    e = explain_with_claude(client, facts)

    assert e.bottom_line == f"Strong run. {DISCLAIMER}"
    assert '"ticker": "NVDA"' in calls[0]["messages"][0]["content"]
    assert calls[0]["output_format"] is StockExplanation
    assert explain_with_claude(fake_client(None, stop_reason="refusal")[0], facts) is None


class FakeMarket:
    def snapshot(self):
        return MARKET

    def lookup(self, ticker):
        return next((r for r in MARKET if r["ticker"] == ticker), None)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(api_main, "market_data", FakeMarket())
    monkeypatch.setattr(api_main, "_sentiment_data", lambda rows, refresh=False: ({"results": NEWS}, 0.0))
    monkeypatch.setattr(api_main, "get_fundamentals", lambda ticker: FUNDAMENTALS)
    monkeypatch.setattr(api_main, "_explain_cache", {})
    return TestClient(api_main.app)


@pytest.fixture
def signed_in():
    """A signed-in caller without a session lookup (sessions live in Firestore)."""
    api_main.app.dependency_overrides[api_main.get_current_user] = lambda: "user-1"
    yield {"Authorization": "Bearer test-session"}
    api_main.app.dependency_overrides.pop(api_main.get_current_user, None)


def test_explain_endpoint_needs_sign_in(client):
    assert client.get("/explain/NVDA").status_code == 401  # rejected before any session lookup


def test_explain_endpoint_uses_rules_without_a_key(client, signed_in):
    res = client.get("/explain/nvda", headers=signed_in)

    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["ticker"], body["source"]) == ("NVDA", "rules")
    assert body["explanation"]["summary"].startswith("NVIDIA Corporation (NVDA)")
    assert body["facts"]["news"]["company_mentions"] == 2
    assert client.get("/explain/ZZZZ", headers=signed_in).status_code == 404


def test_explain_endpoint_ignores_demo_headlines_and_survives_sec_outage(client, signed_in, monkeypatch):
    monkeypatch.setattr(api_main, "_sentiment_data", lambda rows, refresh=False: ({"results": NEWS, "fallback": True}, 0.0))

    def sec_down(ticker):
        raise ConnectionError("SEC unavailable")

    monkeypatch.setattr(api_main, "get_fundamentals", sec_down)

    body = client.get("/explain/NVDA", headers=signed_in).json()

    assert body["facts"]["news"]["company_mentions"] == 0
    assert body["facts"]["fundamentals"] is None
