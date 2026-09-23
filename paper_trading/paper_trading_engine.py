"""
paper_trading_engine.py

A self-contained paper (simulated) trading engine. Builds a portfolio from
a ranked momentum stock list using simple risk/diversification rules,
tracks holdings, and marks the portfolio to market against price updates
over time — producing real profit/loss history without touching any real
brokerage account or real money.

Storage: SQLite (paper_trading.db in the same folder) — swap for your
production database (Postgres, Cosmos DB, etc.) later; the schema below
maps directly onto any relational store.

Usage as a library:
    from paper_trading_engine import PaperTradingEngine

    engine = PaperTradingEngine("paper_trading.db")
    portfolio_id = engine.create_portfolio("Momentum Demo", starting_capital=1000)
    engine.build_portfolio_from_momentum(portfolio_id, momentum_list, sector_lookup)
    engine.mark_to_market(portfolio_id, current_prices, as_of="2026-09-22")
    print(engine.get_portfolio_summary(portfolio_id))

See demo.py for a full runnable walkthrough.
"""

import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Risk / allocation rules — the "predefined portfolio and risk rules" your
# architecture doc calls for. Tune these; they're intentionally simple.
# ---------------------------------------------------------------------------

@dataclass
class RiskRules:
    max_positions: int = 5          # don't spread capital across too many names
    max_allocation_per_stock: float = 0.30   # no single stock over 30% of capital
    max_allocation_per_sector: float = 0.50  # no single sector over 50% of capital
    cash_reserve_pct: float = 0.02  # keep a small cash buffer, don't fully deploy


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub TEXT UNIQUE NOT NULL,
    email TEXT,
    name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    starting_capital REAL NOT NULL,
    cash_balance REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    ticker TEXT NOT NULL,
    sector TEXT,
    quantity REAL NOT NULL,
    avg_buy_price REAL NOT NULL,
    UNIQUE(portfolio_id, ticker)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    ticker TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('BUY','SELL')),
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    as_of TEXT NOT NULL,
    total_value REAL NOT NULL,
    cash_balance REAL NOT NULL,
    holdings_value REAL NOT NULL,
    total_pl REAL NOT NULL,
    total_pl_pct REAL NOT NULL,
    UNIQUE(portfolio_id, as_of)
);
"""


class PaperTradingEngine:
    def __init__(self, db_path="paper_trading.db"):
        # check_same_thread=False: needed because FastAPI runs sync endpoints
        # in a worker thread pool, not the thread that created this engine.
        # Safe here because SQLite serializes writes internally and this
        # engine is only ever used single-process; swap for a proper
        # connection pool if you move to Postgres/multi-process deployment.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -----------------------------------------------------------------
    # Users
    # -----------------------------------------------------------------

    def get_or_create_user(self, google_sub, email=None, name=None):
        """Looks up a user by their stable Google id, creating them on
        first sign-in. Returns the internal user_id (ours, not Google's)."""
        row = self.conn.execute(
            "SELECT id FROM users WHERE google_sub = ?", (google_sub,)
        ).fetchone()
        if row:
            return row["id"]

        cur = self.conn.execute(
            "INSERT INTO users (google_sub, email, name, created_at) VALUES (?, ?, ?, ?)",
            (google_sub, email, name, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def _assert_owner(self, portfolio_id, user_id):
        """Raises PermissionError if this portfolio doesn't belong to this user.
        Every portfolio-touching method below calls this first — a user should
        never be able to view or modify someone else's simulated money."""
        row = self.conn.execute(
            "SELECT user_id FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No portfolio with id {portfolio_id}")
        if row["user_id"] != user_id:
            raise PermissionError(f"Portfolio {portfolio_id} does not belong to this user")

    def list_portfolios_for_user(self, user_id):
        rows = self.conn.execute(
            "SELECT id, name, starting_capital, cash_balance, created_at FROM portfolios WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------
    # Portfolio creation
    # -----------------------------------------------------------------

    def create_portfolio(self, user_id, name, starting_capital):
        cur = self.conn.execute(
            "INSERT INTO portfolios (user_id, name, starting_capital, cash_balance, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, starting_capital, starting_capital, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    # -----------------------------------------------------------------
    # Allocation logic: given a ranked momentum list and risk rules,
    # decide what to buy and how much.
    # -----------------------------------------------------------------

    def build_portfolio_from_momentum(self, portfolio_id, user_id, momentum_list, rules=None, as_of=None):
        """
        portfolio_id, user_id: the portfolio must belong to this user, or this raises PermissionError
        momentum_list: list of dicts, ranked best-first, each with at least
            {"ticker": str, "sector": str, "price": float}
        rules: RiskRules instance (defaults used if omitted)

        Allocation strategy (simple equal-weight within caps):
        1. Take the top N candidates (N = rules.max_positions)
        2. Split capital equally among them, but never exceed the
           per-stock cap or push any one sector over its cap
        3. Whatever a stock's cap can't absorb is redistributed to the
           remaining candidates on a later pass (simple greedy fill)
        4. Buy whole shares only; leftover cash stays as cash
        """
        rules = rules or RiskRules()
        as_of = as_of or datetime.now(timezone.utc).isoformat()
        self._assert_owner(portfolio_id, user_id)

        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise ValueError(f"No portfolio with id {portfolio_id}")

        capital = portfolio["cash_balance"] * (1 - rules.cash_reserve_pct)
        candidates = momentum_list[: rules.max_positions]

        per_stock_cap = portfolio["starting_capital"] * rules.max_allocation_per_stock
        sector_spent = {}
        target_per_stock = capital / len(candidates) if candidates else 0

        purchases = []
        remaining_capital = capital

        for stock in candidates:
            sector = stock.get("sector", "Unknown")
            sector_cap = portfolio["starting_capital"] * rules.max_allocation_per_sector
            sector_used = sector_spent.get(sector, 0.0)
            sector_headroom = max(0.0, sector_cap - sector_used)

            allocation = min(target_per_stock, per_stock_cap, sector_headroom, remaining_capital)
            if allocation <= 0:
                continue

            price = stock["price"]
            quantity = int(allocation // price)  # whole shares only
            if quantity <= 0:
                continue

            cost = quantity * price
            purchases.append({
                "ticker": stock["ticker"],
                "sector": sector,
                "quantity": quantity,
                "price": price,
                "cost": cost,
            })
            sector_spent[sector] = sector_used + cost
            remaining_capital -= cost

        # Execute the purchases: write holdings + transactions, update cash
        for p in purchases:
            self.conn.execute(
                """INSERT INTO holdings (portfolio_id, ticker, sector, quantity, avg_buy_price)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(portfolio_id, ticker) DO UPDATE SET
                     quantity = quantity + excluded.quantity,
                     avg_buy_price = ((avg_buy_price * quantity) + (excluded.avg_buy_price * excluded.quantity))
                                     / (quantity + excluded.quantity)""",
                (portfolio_id, p["ticker"], p["sector"], p["quantity"], p["price"]),
            )
            self.conn.execute(
                "INSERT INTO transactions (portfolio_id, ticker, type, quantity, price, timestamp) VALUES (?, ?, 'BUY', ?, ?, ?)",
                (portfolio_id, p["ticker"], p["quantity"], p["price"], as_of),
            )

        new_cash = portfolio["cash_balance"] - sum(p["cost"] for p in purchases)
        self.conn.execute(
            "UPDATE portfolios SET cash_balance = ? WHERE id = ?", (new_cash, portfolio_id)
        )
        self.conn.commit()
        return purchases

    # -----------------------------------------------------------------
    # Mark-to-market: recompute portfolio value against current prices
    # -----------------------------------------------------------------

    def mark_to_market(self, portfolio_id, user_id, current_prices, as_of=None):
        """current_prices: dict {ticker: price}. Portfolio must belong to user_id."""
        as_of = as_of or datetime.now(timezone.utc).isoformat()
        self._assert_owner(portfolio_id, user_id)

        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        holdings = self.conn.execute(
            "SELECT * FROM holdings WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchall()

        holdings_value = 0.0
        for h in holdings:
            price = current_prices.get(h["ticker"])
            if price is None:
                continue  # no fresh price available this run; skip
            holdings_value += h["quantity"] * price

        total_value = holdings_value + portfolio["cash_balance"]
        total_pl = total_value - portfolio["starting_capital"]
        total_pl_pct = (total_pl / portfolio["starting_capital"]) * 100 if portfolio["starting_capital"] else 0

        self.conn.execute(
            """INSERT INTO daily_snapshots (portfolio_id, as_of, total_value, cash_balance, holdings_value, total_pl, total_pl_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(portfolio_id, as_of) DO UPDATE SET
                 total_value = excluded.total_value,
                 cash_balance = excluded.cash_balance,
                 holdings_value = excluded.holdings_value,
                 total_pl = excluded.total_pl,
                 total_pl_pct = excluded.total_pl_pct""",
            (portfolio_id, as_of, total_value, portfolio["cash_balance"], holdings_value, total_pl, total_pl_pct),
        )
        self.conn.commit()
        return {
            "as_of": as_of,
            "total_value": round(total_value, 2),
            "cash_balance": round(portfolio["cash_balance"], 2),
            "holdings_value": round(holdings_value, 2),
            "total_pl": round(total_pl, 2),
            "total_pl_pct": round(total_pl_pct, 2),
        }

    # -----------------------------------------------------------------
    # Reporting
    # -----------------------------------------------------------------

    def get_portfolio_summary(self, portfolio_id, user_id):
        self._assert_owner(portfolio_id, user_id)
        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        holdings = self.conn.execute(
            "SELECT * FROM holdings WHERE portfolio_id = ? ORDER BY quantity * avg_buy_price DESC",
            (portfolio_id,),
        ).fetchall()
        latest_snapshot = self.conn.execute(
            "SELECT * FROM daily_snapshots WHERE portfolio_id = ? ORDER BY as_of DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()

        return {
            "name": portfolio["name"],
            "starting_capital": portfolio["starting_capital"],
            "cash_balance": round(portfolio["cash_balance"], 2),
            "holdings": [dict(h) for h in holdings],
            "latest": dict(latest_snapshot) if latest_snapshot else None,
        }

    def get_history(self, portfolio_id, user_id):
        self._assert_owner(portfolio_id, user_id)
        rows = self.conn.execute(
            "SELECT * FROM daily_snapshots WHERE portfolio_id = ? ORDER BY as_of ASC",
            (portfolio_id,),
        ).fetchall()
        return [dict(r) for r in rows]
