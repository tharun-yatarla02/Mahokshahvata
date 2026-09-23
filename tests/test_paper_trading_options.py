from paper_trading.paper_trading_engine import PaperTradingEngine, RiskRules


def test_trade_option_records_timestamps_and_history(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    engine = PaperTradingEngine(str(db_path))
    user_id = engine.get_or_create_user("demo-user", "demo@example.com", "Demo User")
    portfolio_id = engine.create_portfolio(user_id, "Options Demo", 5000)

    trade = engine.trade_option(
        portfolio_id,
        user_id,
        ticker="AAPL",
        option_type="CALL",
        strike=200.0,
        expiry="2026-12-18",
        quantity=2,
        premium=8.5,
        side="BUY",
    )

    assert trade["instrument_type"] == "OPTION"
    assert trade["option_type"] == "CALL"
    assert trade["timestamp"]
    assert trade["quantity"] == 2

    history = engine.get_trade_history(portfolio_id, user_id)
    assert len(history) == 1
    assert history[0]["ticker"] == "AAPL"
    assert history[0]["timestamp"] == trade["timestamp"]


def test_option_mark_to_market_uses_current_premium(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    engine = PaperTradingEngine(str(db_path))
    user_id = engine.get_or_create_user("option-user", "user@example.com", "Option User")
    portfolio_id = engine.create_portfolio(user_id, "Option Portfolio", 2500)

    engine.trade_option(
        portfolio_id,
        user_id,
        ticker="NVDA",
        option_type="PUT",
        strike=900.0,
        expiry="2026-11-20",
        quantity=3,
        premium=12.0,
        side="BUY",
    )

    snapshot = engine.mark_to_market(portfolio_id, user_id, {"NVDA": 150.0}, as_of="2026-09-23T10:00:00Z")
    assert snapshot["holdings_value"] >= 0
    assert snapshot["cash_balance"] == 2464.0


def test_add_cash_balance_updates_portfolio_and_summary(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    engine = PaperTradingEngine(str(db_path))
    user_id = engine.get_or_create_user("balance-user", "balance@example.com", "Balance User")
    portfolio_id = engine.create_portfolio(user_id, "Cash Portfolio", 1000)

    engine.add_cash_balance(portfolio_id, user_id, 500)
    summary = engine.get_portfolio_summary(portfolio_id, user_id)

    assert summary["cash_balance"] == 1500.0
    assert summary["remaining_balance_to_invest"] == 1500.0
    assert summary["total_invested"] == 0.0
    assert summary["stock_count"] == 0


def test_mark_to_market_does_not_treat_cash_topups_as_profit(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    engine = PaperTradingEngine(str(db_path))
    user_id = engine.get_or_create_user("pnl-user", "pnl@example.com", "P&L User")
    portfolio_id = engine.create_portfolio(user_id, "P&L Portfolio", 1000)

    engine.add_cash_balance(portfolio_id, user_id, 500)
    snapshot = engine.mark_to_market(portfolio_id, user_id, {}, as_of="2026-09-23T10:00:00Z")

    assert snapshot["total_pl"] == 0.0
    assert snapshot["total_pl_pct"] == 0.0


def test_summary_percentages_and_position_weights_are_consistent(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    engine = PaperTradingEngine(str(db_path))
    user_id = engine.get_or_create_user("math-user", "math@example.com", "Math User")
    portfolio_id = engine.create_portfolio(user_id, "Math Portfolio", 10000)

    engine.conn.execute(
        "INSERT INTO holdings (portfolio_id, ticker, sector, instrument_type, position_key, quantity, avg_buy_price) VALUES (?, ?, ?, 'STOCK', ?, 10, 100.0)",
        (portfolio_id, "AAPL", "Technology", "AAPL"),
    )
    engine.conn.execute(
        "INSERT INTO holdings (portfolio_id, ticker, sector, instrument_type, position_key, quantity, avg_buy_price) VALUES (?, ?, ?, 'STOCK', ?, 5, 200.0)",
        (portfolio_id, "MSFT", "Technology", "MSFT"),
    )
    engine.conn.execute(
        "INSERT INTO holdings (portfolio_id, ticker, sector, instrument_type, position_key, quantity, avg_buy_price) VALUES (?, ?, ?, 'STOCK', ?, 8, 75.0)",
        (portfolio_id, "XOM", "Energy", "XOM"),
    )
    engine.conn.commit()

    summary = engine.get_portfolio_summary(portfolio_id, user_id)
    total_invested = summary["total_invested"]
    assert total_invested == 1000.0 + 1000.0 + 600.0
    assert summary["sector_breakdown"]["Technology"] == 2000.0
    assert summary["sector_breakdown"]["Energy"] == 600.0
    assert summary["stock_count"] == 3
    assert round((summary["sector_breakdown"]["Technology"] / total_invested) * 100, 2) == 76.92
    assert round((summary["sector_breakdown"]["Energy"] / total_invested) * 100, 2) == 23.08


def test_option_trade_cost_scales_with_quantity_and_premium(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    engine = PaperTradingEngine(str(db_path))
    user_id = engine.get_or_create_user("option-math-user", "optionmath@example.com", "Option Math User")
    portfolio_id = engine.create_portfolio(user_id, "Option Math Portfolio", 2500)

    trade = engine.trade_option(
        portfolio_id,
        user_id,
        ticker="AAPL",
        option_type="CALL",
        strike=200.0,
        expiry="2026-12-18",
        quantity=2,
        premium=8.5,
        side="BUY",
    )

    summary = engine.get_portfolio_summary(portfolio_id, user_id)
    assert trade["quantity"] == 2
    assert trade["premium"] == 8.5
    assert summary["cash_balance"] == 2483.0
    assert summary["remaining_balance_to_invest"] == 2483.0
