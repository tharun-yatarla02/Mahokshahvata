"""Autopilot decisions: sells, buys, guardrails and the kill switch."""
from autopilot.autopilot import clamp_settings, plan_run


def row(ticker, price, momentum, strength, sector="Technology"):
    return {"ticker": ticker, "current_price": price, "momentum_pct": momentum,
            "relative_strength": strength, "sector": sector}


NO_HEADLINES = {}


def plan(holdings=(), managed=(), universe=(), news=NO_HEADLINES, cash=10_000, total=10_000, reference=None, **settings):
    rows = {r["ticker"]: r for r in universe}
    return plan_run(settings, cash, total, list(holdings), list(managed), rows.get, list(universe), news, reference)


def sides(result):
    return [(t["side"], t["ticker"]) for t in result["trades"]]


def test_buys_the_strongest_rising_stocks_with_equal_capped_positions():
    universe = [row("AAA", 100, 20, 15), row("BBB", 50, 30, 25), row("CCC", 10, 5, 2),
                row("DOWN", 20, -5, -10), row("LAG", 20, 3, -1)]
    result = plan(universe=universe, max_positions=2, cash_reserve_pct=10, max_position_pct=40)
    assert sides(result) == [("BUY", "BBB"), ("BUY", "AAA")]          # strongest vs S&P 500 first
    assert all(t["value"] <= 4000 for t in result["trades"])            # 40% cap of $10,000
    assert sum(t["value"] for t in result["trades"]) <= 9000           # 10% cash reserve kept


def test_good_news_first_and_negative_news_never_bought():
    universe = [row("AAA", 100, 20, 30), row("BBB", 100, 10, 5), row("BAD", 100, 40, 50)]
    news = {"BBB": {"tone_label": "Positive", "view": "Good"}, "BAD": {"tone_label": "Negative", "view": "Wait"}}
    result = plan(universe=universe, news=news, max_positions=2)
    assert sides(result) == [("BUY", "BBB"), ("BUY", "AAA")]


def test_sells_only_positions_it_manages():
    holdings = [{"ticker": "MINE", "quantity": 10, "avg_buy_price": 100},
                {"ticker": "AUTO", "quantity": 10, "avg_buy_price": 100}]
    universe = [row("MINE", 50, -30, -40), row("AUTO", 50, -30, -40)]   # both crashed
    result = plan(holdings=holdings, managed=["AUTO"], universe=universe, max_positions=1)
    assert sides(result) == [("SELL", "AUTO")]
    assert "Stop-loss" in result["trades"][0]["reason"]


def test_sell_reasons_trend_lagging_and_news():
    holdings = [{"ticker": t, "quantity": 1, "avg_buy_price": 100} for t in ("TREND", "LAG", "NEWS", "OK")]
    universe = [row("TREND", 99, -2, 1), row("LAG", 101, 4, -12), row("NEWS", 101, 4, -1), row("OK", 105, 8, 6)]
    news = {"NEWS": {"tone_label": "Negative", "view": "Wait"}}
    result = plan(holdings=holdings, managed=["TREND", "LAG", "NEWS", "OK"], universe=universe, news=news)
    reasons = {t["ticker"]: t["reason"] for t in result["trades"] if t["side"] == "SELL"}
    assert set(reasons) == {"TREND", "LAG", "NEWS"}
    assert "Trend turned down" in reasons["TREND"]
    assert "behind the S&P 500" in reasons["LAG"]
    assert "Negative news" in reasons["NEWS"]


def test_kill_switch_pauses_after_a_big_daily_drop():
    universe = [row("AAA", 100, 20, 15)]
    paused = plan(universe=universe, total=9_400, reference=10_000, daily_loss_limit_pct=5)
    assert paused["status"] == "paused" and paused["trades"] == []
    assert "Daily loss limit" in paused["notes"][0]
    assert plan(universe=universe, total=9_600, reference=10_000, daily_loss_limit_pct=5)["status"] == "ok"


def test_guardrails_max_positions_trades_and_reserve():
    universe = [row(f"S{i}", 10, 10 + i, 5 + i) for i in range(10)]
    assert len(plan(universe=universe, max_positions=3)["trades"]) == 3
    assert len(plan(universe=universe, max_trades_per_run=2)["trades"]) == 2
    full = plan(universe=universe, cash=400, total=10_000, cash_reserve_pct=5)   # $400 < $500 reserve
    assert full["trades"] == [] and "reserve" in full["notes"][0]


def test_does_not_buy_what_the_user_already_holds():
    holdings = [{"ticker": "AAA", "quantity": 1, "avg_buy_price": 100}]
    result = plan(holdings=holdings, universe=[row("AAA", 100, 20, 15), row("BBB", 100, 10, 5)], cash=9_900)
    assert sides(result) == [("BUY", "BBB")]


def test_settings_are_clamped_and_defaulted():
    s = clamp_settings({"max_positions": 99, "stop_loss_pct": 0, "use_news": 0, "unknown": 5})
    assert s["max_positions"] == 20 and s["stop_loss_pct"] == 1 and s["use_news"] is False
    assert "unknown" not in s and s["cash_reserve_pct"] == 5


def test_without_news_it_says_so_and_uses_trends_only():
    result = plan(universe=[row("AAA", 100, 20, 15)], news=None)
    assert sides(result) == [("BUY", "AAA")] and "News isn't available" in result["notes"][0]
