import sqlite3

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


def test_trade_stock_buy_sell_updates_cash_and_summary(tmp_path):
    db_path = tmp_path / "paper_trading.db"
    engine = PaperTradingEngine(str(db_path))
    user_id = engine.get_or_create_user("stock-trade-user", "stock@example.com", "Stock Trade User")
    portfolio_id = engine.create_portfolio(user_id, "Stock Trading Portfolio", 10000)

    buy = engine.trade_stock(
        portfolio_id,
        user_id,
        ticker="AAPL",
        quantity=10,
        price=150.0,
        side="BUY",
    )
    assert buy["quantity"] == 10
    assert buy["cash_balance"] == 8500.0

    sell = engine.trade_stock(
        portfolio_id,
        user_id,
        ticker="AAPL",
        quantity=4,
        price=170.0,
        side="SELL",
    )
    assert sell["quantity"] == 4
    assert sell["cash_balance"] == 9180.0

    summary = engine.get_portfolio_summary(portfolio_id, user_id)
    assert summary["cash_balance"] == 9180.0
    assert summary["remaining_balance_to_invest"] == 9180.0
    assert summary["total_invested"] == 900.0
    assert summary["stock_count"] == 1
    assert summary["total_portfolio_value"] == 10080.0
    assert summary["total_pl"] == 80.0  # sold 4 shares $20 above cost


def test_legacy_database_allows_stock_and_option_on_same_ticker(tmp_path):
    # Databases created before options support were unique on (portfolio_id, ticker).
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, firebase_uid TEXT UNIQUE NOT NULL,
                            email TEXT, name TEXT, created_at TEXT NOT NULL);
        CREATE TABLE portfolios (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                 name TEXT NOT NULL, starting_capital REAL NOT NULL,
                                 cash_balance REAL NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE holdings (id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio_id INTEGER NOT NULL,
                               ticker TEXT NOT NULL, sector TEXT, quantity REAL NOT NULL,
                               avg_buy_price REAL NOT NULL, UNIQUE(portfolio_id, ticker));
        INSERT INTO users VALUES (1, 'demo-user', 'demo@example.com', 'Demo User', '2026-01-01');
        INSERT INTO portfolios VALUES (1, 1, 'Legacy', 5000, 4700, '2026-01-01');
        INSERT INTO holdings (portfolio_id, ticker, sector, quantity, avg_buy_price)
            VALUES (1, 'AAPL', 'Technology', 2, 150);
        """
    )
    conn.close()

    engine = PaperTradingEngine(str(db_path))
    engine.trade_option(1, 1, ticker="AAPL", option_type="CALL", strike=200.0,
                        expiry="2026-12-18", quantity=2, premium=8.5, side="BUY")

    holdings = engine.get_portfolio_summary(1, 1)["holdings"]
    assert sorted(h["instrument_type"] for h in holdings if h["ticker"] == "AAPL") == ["OPTION", "STOCK"]
    stock = next(h for h in holdings if h["instrument_type"] == "STOCK")
    assert stock["quantity"] == 2


def test_cash_stays_exact_to_the_cent_after_many_trades(tmp_path):
    engine = PaperTradingEngine(str(tmp_path / "cents.db"))
    user = engine.get_or_create_user("u1")
    pid = engine.create_portfolio(user, "Cents", 100)

    for _ in range(3):
        engine.trade_stock(pid, user, "AAPL", 1, 33.33)  # 3 x 33.33 = 99.99 exactly

    assert engine.get_portfolio_summary(pid, user)["cash_balance"] == 0.01
    raw = engine.conn.execute("SELECT cash_balance FROM portfolios WHERE id = ?", (pid,)).fetchone()[0]
    assert raw == 0.01


def test_trade_stock_records_sector_and_backfills_unknown(tmp_path):
    engine = PaperTradingEngine(str(tmp_path / "paper_trading.db"))
    user_id = engine.get_or_create_user("demo-user", "demo@example.com", "Demo User")
    portfolio_id = engine.create_portfolio(user_id, "Sectors", 5000)

    engine.trade_stock(portfolio_id, user_id, "PODD", 1, 100.0, sector="Healthcare")
    engine.trade_stock(portfolio_id, user_id, "AAPL", 1, 100.0)  # no sector known yet

    sectors = lambda: {h["ticker"]: h["sector"] for h in engine.get_portfolio_summary(portfolio_id, user_id)["holdings"]}
    assert sectors() == {"PODD": "Healthcare", "AAPL": "Unknown"}

    updated = engine.fill_unknown_sectors({"AAPL": "Technology"}.get)

    assert updated == 1
    assert sectors() == {"PODD": "Healthcare", "AAPL": "Technology"}


def _engine_with_portfolio(tmp_path, capital=1000):
    engine = PaperTradingEngine(str(tmp_path / "pl.db"))
    user = engine.get_or_create_user("pl-user")
    return engine, user, engine.create_portfolio(user, "P/L", capital)


def test_pl_counts_realized_and_live_unrealized_gains(tmp_path):
    engine, user, pid = _engine_with_portfolio(tmp_path)
    engine.trade_stock(pid, user, "AAPL", 2, 100)
    engine.trade_stock(pid, user, "AAPL", 1, 150, side="SELL")   # +50 realized

    summary = engine.get_portfolio_summary(pid, user, price_for={"AAPL": 120.0}.get)  # +20 unrealized

    assert summary["total_portfolio_value"] == 1070.0
    assert summary["total_pl"] == 70.0
    assert summary["total_pl_pct"] == 7.0
    assert summary["holdings"][0]["current_price"] == 120.0
    assert summary["holdings"][0]["market_value"] == 120.0


def test_deposits_are_not_profit_and_options_keep_their_value(tmp_path):
    engine, user, pid = _engine_with_portfolio(tmp_path)
    engine.add_cash_balance(pid, user, 500)
    engine.trade_option(pid, user, "aapl", "CALL", 200, "2026-12-18", 2, 10)

    summary = engine.get_portfolio_summary(pid, user)

    assert summary["contributed_capital"] == 1500.0
    assert summary["total_portfolio_value"] == 1500.0   # the $20 option is still worth its cost
    assert summary["total_invested"] == 20.0
    assert summary["total_pl"] == 0.0
    assert summary["holdings"][0]["ticker"] == "AAPL"


def test_summary_works_after_mark_to_market(tmp_path):
    engine, user, pid = _engine_with_portfolio(tmp_path)
    engine.trade_stock(pid, user, "MSFT", 2, 100)
    engine.trade_stock(pid, user, "NVDA", 1, 100)

    snapshot = engine.mark_to_market(pid, user, {"MSFT": 110})   # no NVDA quote: stays at cost

    assert snapshot["holdings_value"] == 320.0
    assert snapshot["total_pl"] == 20.0
    assert engine.get_portfolio_summary(pid, user)["latest"]["total_pl"] == 20.0


def test_contributed_capital_is_backfilled_for_existing_portfolios(tmp_path):
    engine, user, pid = _engine_with_portfolio(tmp_path)
    engine.trade_stock(pid, user, "AAPL", 2, 100)
    engine.trade_stock(pid, user, "AAPL", 1, 150, side="SELL")
    # Simulate a database from before deposits were tracked: an untracked $300 deposit.
    engine.conn.execute("UPDATE portfolios SET cash_balance = cash_balance + 300, contributed_capital = NULL")
    engine.conn.commit()

    reopened = PaperTradingEngine(str(tmp_path / "pl.db"))

    summary = reopened.get_portfolio_summary(pid, user)
    assert summary["contributed_capital"] == 1300.0
    assert summary["total_pl"] == 50.0


def test_sector_breakdown_uses_market_value_and_counts_stock_sectors_only(tmp_path):
    engine, user, pid = _engine_with_portfolio(tmp_path, capital=10000)
    engine.trade_stock(pid, user, "AAPL", 10, 100, sector="Technology")
    engine.trade_stock(pid, user, "XOM", 10, 100, sector="Energy")
    engine.trade_option(pid, user, "AAPL", "CALL", 200, "2099-12-18", 1, 50)

    summary = engine.get_portfolio_summary(pid, user, price_for={"AAPL": 150.0, "XOM": 90.0}.get)

    assert summary["sector_breakdown"] == {"Energy": 900.0, "Options": 50.0, "Technology": 1500.0}
    assert sum(summary["sector_breakdown"].values()) == summary["holdings_value"] == 2450.0
    assert summary["sector_count"] == 2
    assert summary["total_pl"] == 400.0   # +500 AAPL, -100 XOM, option at cost


def test_daily_snapshot_is_one_row_per_day(tmp_path):
    engine, user, pid = _engine_with_portfolio(tmp_path)
    summary = engine.get_portfolio_summary(pid, user)
    engine.record_daily_snapshot(pid, summary, as_of="2026-09-22")
    engine.record_daily_snapshot(pid, summary, as_of="2026-09-23")
    engine.record_daily_snapshot(pid, {**summary, "total_portfolio_value": 1234.0}, as_of="2026-09-23")

    history = engine.get_history(pid, user)

    assert [(h["as_of"], h["total_value"]) for h in history] == [("2026-09-22", 1000.0), ("2026-09-23", 1234.0)]
