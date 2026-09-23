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

import functools
import re
import sqlite3
import threading
from datetime import datetime, timezone
from dataclasses import dataclass


def _locked(method):
    """Runs the method under the engine's lock. FastAPI calls the engine from
    several worker threads over one shared connection, so without this two
    BUYs can both pass the cash check, and one thread's commit() can commit
    another thread's half-finished writes."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _normalize_timestamp(ts=None):
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return ts


def _option_position_key(ticker, option_type, strike, expiry):
    return f"{ticker}|{option_type}|{strike}|{expiry}"


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
    firebase_uid TEXT UNIQUE NOT NULL,
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
    instrument_type TEXT DEFAULT 'STOCK' CHECK(instrument_type IN ('STOCK', 'OPTION')),
    position_key TEXT DEFAULT '',
    option_type TEXT,
    strike REAL,
    expiry TEXT,
    quantity REAL NOT NULL,
    avg_buy_price REAL NOT NULL,
    UNIQUE(portfolio_id, position_key)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    ticker TEXT NOT NULL,
    instrument_type TEXT DEFAULT 'STOCK' CHECK(instrument_type IN ('STOCK', 'OPTION')),
    option_type TEXT,
    strike REAL,
    expiry TEXT,
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
        # Every public method takes self._lock (see _locked), so the shared
        # connection is used by one thread at a time.
        # ponytail: one lock per process serializes all trades; move to
        # per-request connections + BEGIN IMMEDIATE (or Postgres) for multi-process.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self):
        for table_name, columns in {
            "holdings": [
                ("instrument_type", "TEXT DEFAULT 'STOCK'"),
                ("position_key", "TEXT DEFAULT ''"),
                ("option_type", "TEXT"),
                ("strike", "REAL"),
                ("expiry", "TEXT"),
            ],
            "transactions": [
                ("instrument_type", "TEXT DEFAULT 'STOCK'"),
                ("option_type", "TEXT"),
                ("strike", "REAL"),
                ("expiry", "TEXT"),
            ],
        }.items():
            existing = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            existing_columns = {col[1] for col in existing}
            for column_name, column_def in columns:
                if column_name not in existing_columns:
                    self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

        self.conn.execute("UPDATE holdings SET instrument_type = 'STOCK' WHERE instrument_type IS NULL")
        self.conn.execute("UPDATE holdings SET position_key = ticker WHERE position_key IS NULL OR position_key = ''")
        self.conn.execute("UPDATE transactions SET instrument_type = 'STOCK' WHERE instrument_type IS NULL")

        holdings_rows = self.conn.execute("SELECT id, ticker, position_key, instrument_type FROM holdings").fetchall()
        for row in holdings_rows:
            if not row["position_key"]:
                self.conn.execute(
                    "UPDATE holdings SET position_key = ? WHERE id = ?",
                    (row["ticker"], row["id"]),
                )

        self._rebuild_legacy_holdings_table()
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_holdings_portfolio_position ON holdings(portfolio_id, position_key)")

    def _rebuild_legacy_holdings_table(self):
        """Databases created before options support have UNIQUE(portfolio_id, ticker)
        on holdings, which blocks holding a stock and an option on the same ticker.
        SQLite can't drop a table constraint, so copy the rows into a fresh table
        built from SCHEMA (unique on position_key instead)."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'holdings'"
        ).fetchone()
        if not row or not re.search(r"UNIQUE\s*\(\s*portfolio_id\s*,\s*ticker\s*\)", row[0]):
            return

        columns = (
            "id, portfolio_id, ticker, sector, instrument_type, position_key, "
            "option_type, strike, expiry, quantity, avg_buy_price"
        )
        # SCHEMA is all IF NOT EXISTS, so re-running it only recreates holdings.
        self.conn.executescript(f"""
            BEGIN;
            ALTER TABLE holdings RENAME TO holdings_legacy;
            {SCHEMA}
            INSERT INTO holdings ({columns}) SELECT {columns} FROM holdings_legacy;
            DROP TABLE holdings_legacy;
            COMMIT;
        """)

    # -----------------------------------------------------------------
    # Users
    # -----------------------------------------------------------------

    @_locked
    def get_or_create_user(self, firebase_uid, email=None, name=None):
        """Looks up a user by their stable Firebase id, creating them on
        first sign-in. Returns the internal user_id (ours, not Firebase's)."""
        row = self.conn.execute(
            "SELECT id FROM users WHERE firebase_uid = ?", (firebase_uid,)
        ).fetchone()
        if row:
            return row["id"]

        cur = self.conn.execute(
            "INSERT INTO users (firebase_uid, email, name, created_at) VALUES (?, ?, ?, ?)",
            (firebase_uid, email, name, datetime.now(timezone.utc).isoformat()),
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

    @_locked
    def list_portfolios_for_user(self, user_id):
        rows = self.conn.execute(
            "SELECT id, name, starting_capital, cash_balance, created_at FROM portfolios WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------
    # Portfolio creation
    # -----------------------------------------------------------------

    @_locked
    def create_portfolio(self, user_id, name, starting_capital):
        # Cash is kept rounded to cents at every write so float error can't
        # accumulate across trades (e.g. 0.1 + 0.2 != 0.3).
        # ponytail: REAL columns + rounding; switch to integer cents if exact
        # accounting (tax lots, fees, fractional shares) is ever needed.
        starting_capital = round(starting_capital, 2)
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

    def _get_position_key(self, ticker, instrument_type="STOCK", option_type=None, strike=None, expiry=None):
        if instrument_type == "OPTION":
            return _option_position_key(ticker, option_type, strike, expiry)
        return ticker

    @_locked
    def build_portfolio_from_momentum(self, portfolio_id, user_id, momentum_list, rules=None, as_of=None):
        """
        portfolio_id, user_id: the portfolio must belong to this user, or this raises PermissionError
        momentum_list: list of dicts, ranked best-first, each with at least
            {"ticker": str, "sector": str, "price": float}
        rules: RiskRules instance (defaults used if omitted)

        Allocation strategy (iterative waterfill, equal-weight within caps):
        1. Take the top N candidates (N = rules.max_positions)
        2. Split remaining capital equally among candidates that can still
           afford at least one more share within their remaining per-stock
           and per-sector headroom
        3. Repeat: whatever a stock's cap or price couldn't absorb this
           round gets redistributed among the remaining affordable
           candidates on the next round, instead of being abandoned —
           this is what stops small starting capital from silently
           under-deploying into only 1-2 positions when the "fair share"
           per stock can't afford a whole share of a pricier candidate
        4. Stops when no candidate can afford another share, or the caps
           are exhausted. Buy whole shares only; leftover cash stays as cash.
        """
        rules = rules or RiskRules()
        as_of = _normalize_timestamp(as_of)
        self._assert_owner(portfolio_id, user_id)

        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise ValueError(f"No portfolio with id {portfolio_id}")

        candidates = momentum_list[: rules.max_positions]
        if not candidates:
            return []

        per_stock_cap = portfolio["starting_capital"] * rules.max_allocation_per_stock
        sector_cap = portfolio["starting_capital"] * rules.max_allocation_per_sector

        remaining_capital = portfolio["cash_balance"] * (1 - rules.cash_reserve_pct)
        stock_spent = {c["ticker"]: 0.0 for c in candidates}
        sector_spent = {}
        purchased_qty = {c["ticker"]: 0 for c in candidates}

        # Iterative waterfill: keep redistributing remaining capital across
        # whichever candidates can still afford another share, until no
        # round makes progress.
        made_progress = True
        while made_progress and remaining_capital > 0:
            made_progress = False

            active = []
            for stock in candidates:
                ticker = stock["ticker"]
                sector = stock.get("sector", "Unknown")
                stock_headroom = per_stock_cap - stock_spent[ticker]
                sector_headroom = sector_cap - sector_spent.get(sector, 0.0)
                if stock_headroom >= stock["price"] and sector_headroom >= stock["price"] and remaining_capital >= stock["price"]:
                    active.append(stock)

            if not active:
                break

            share = remaining_capital / len(active)

            for stock in active:
                ticker = stock["ticker"]
                sector = stock.get("sector", "Unknown")
                price = stock["price"]
                stock_headroom = per_stock_cap - stock_spent[ticker]
                sector_headroom = sector_cap - sector_spent.get(sector, 0.0)

                allocation = min(share, stock_headroom, sector_headroom, remaining_capital)
                quantity = int(allocation // price)  # whole shares only
                if quantity <= 0:
                    continue

                cost = quantity * price
                stock_spent[ticker] += cost
                sector_spent[sector] = sector_spent.get(sector, 0.0) + cost
                remaining_capital -= cost
                purchased_qty[ticker] += quantity
                made_progress = True

        purchases = []
        for stock in candidates:
            ticker = stock["ticker"]
            qty = purchased_qty[ticker]
            if qty <= 0:
                continue
            purchases.append({
                "ticker": ticker,
                "sector": stock.get("sector", "Unknown"),
                "quantity": qty,
                "price": stock["price"],
                "cost": stock_spent[ticker],
            })

        # Execute the purchases: write holdings + transactions, update cash
        for p in purchases:
            position_key = self._get_position_key(p["ticker"], instrument_type="STOCK")
            self.conn.execute(
                """INSERT INTO holdings (portfolio_id, ticker, sector, instrument_type, position_key, quantity, avg_buy_price)
                   VALUES (?, ?, ?, 'STOCK', ?, ?, ?)
                   ON CONFLICT(portfolio_id, position_key) DO UPDATE SET
                     quantity = quantity + excluded.quantity,
                     avg_buy_price = ((avg_buy_price * quantity) + (excluded.avg_buy_price * excluded.quantity))
                                     / (quantity + excluded.quantity)""",
                (portfolio_id, p["ticker"], p["sector"], position_key, p["quantity"], p["price"]),
            )
            self.conn.execute(
                "INSERT INTO transactions (portfolio_id, ticker, instrument_type, type, quantity, price, timestamp) VALUES (?, ?, 'STOCK', 'BUY', ?, ?, ?)",
                (portfolio_id, p["ticker"], p["quantity"], p["price"], as_of),
            )

        new_cash = round(portfolio["cash_balance"] - sum(p["cost"] for p in purchases), 2)
        self.conn.execute(
            "UPDATE portfolios SET cash_balance = ? WHERE id = ?", (new_cash, portfolio_id)
        )
        self.conn.commit()
        return purchases

    @_locked
    def trade_stock(self, portfolio_id, user_id, ticker, quantity, price, side="BUY", timestamp=None, sector="Unknown"):
        self._assert_owner(portfolio_id, user_id)
        ticker = (ticker or "").strip().upper()
        if not ticker:
            raise ValueError("Ticker is required")
        if quantity <= 0:
            raise ValueError("Stock quantity must be positive")
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        trade_price = float(price)
        if trade_price <= 0:
            raise ValueError("Stock price must be positive")

        ts = _normalize_timestamp(timestamp)
        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise ValueError(f"No portfolio with id {portfolio_id}")

        trade_value = round(quantity * trade_price, 2)
        position_key = self._get_position_key(ticker, instrument_type="STOCK")
        existing = self.conn.execute(
            "SELECT * FROM holdings WHERE portfolio_id = ? AND position_key = ?",
            (portfolio_id, position_key),
        ).fetchone()

        if side == "BUY":
            if portfolio["cash_balance"] < trade_value:
                raise ValueError("Insufficient cash to buy this stock")
            self.conn.execute(
                """INSERT INTO holdings (portfolio_id, ticker, sector, instrument_type, position_key, quantity, avg_buy_price)
                   VALUES (?, ?, ?, 'STOCK', ?, ?, ?)
                   ON CONFLICT(portfolio_id, position_key) DO UPDATE SET
                     sector = CASE WHEN sector = 'Unknown' THEN excluded.sector ELSE sector END,
                     quantity = quantity + excluded.quantity,
                     avg_buy_price = ((avg_buy_price * quantity) + (excluded.avg_buy_price * excluded.quantity))
                                     / (quantity + excluded.quantity)""",
                (portfolio_id, ticker, sector, position_key, quantity, trade_price),
            )
            self.conn.execute(
                "UPDATE portfolios SET cash_balance = ROUND(cash_balance - ?, 2) WHERE id = ?",
                (trade_value, portfolio_id),
            )
        else:
            if existing is None or existing["quantity"] < quantity:
                raise ValueError("Not enough shares to sell")
            remaining_qty = existing["quantity"] - quantity
            if remaining_qty <= 0:
                self.conn.execute("DELETE FROM holdings WHERE portfolio_id = ? AND position_key = ?", (portfolio_id, position_key))
            else:
                self.conn.execute(
                    "UPDATE holdings SET quantity = ? WHERE portfolio_id = ? AND position_key = ?",
                    (remaining_qty, portfolio_id, position_key),
                )
            self.conn.execute(
                "UPDATE portfolios SET cash_balance = ROUND(cash_balance + ?, 2) WHERE id = ?",
                (trade_value, portfolio_id),
            )

        self.conn.execute(
            "INSERT INTO transactions (portfolio_id, ticker, instrument_type, type, quantity, price, timestamp) VALUES (?, ?, 'STOCK', ?, ?, ?, ?)",
            (portfolio_id, ticker, side, quantity, trade_price, ts),
        )
        self.conn.commit()

        cash_after = round(float(self.conn.execute("SELECT cash_balance FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()[0]), 2)
        return {
            "portfolio_id": portfolio_id,
            "ticker": ticker,
            "instrument_type": "STOCK",
            "quantity": quantity,
            "price": round(float(trade_price), 2),
            "side": side,
            "timestamp": ts,
            "cash_balance": cash_after,
        }

    @_locked
    def trade_option(self, portfolio_id, user_id, ticker, option_type, strike, expiry, quantity, premium, side="BUY", timestamp=None):
        self._assert_owner(portfolio_id, user_id)
        if quantity <= 0:
            raise ValueError("Option quantity must be positive")
        if option_type not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        ts = _normalize_timestamp(timestamp)
        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise ValueError(f"No portfolio with id {portfolio_id}")

        trade_cost = round(quantity * premium, 2)
        position_key = self._get_position_key(ticker, "OPTION", option_type, strike, expiry)
        existing = self.conn.execute(
            "SELECT * FROM holdings WHERE portfolio_id = ? AND position_key = ?",
            (portfolio_id, position_key),
        ).fetchone()

        if side == "BUY":
            if portfolio["cash_balance"] < trade_cost:
                raise ValueError("Insufficient cash to buy this option")
            self.conn.execute(
                """INSERT INTO holdings (portfolio_id, ticker, sector, instrument_type, position_key, option_type, strike, expiry, quantity, avg_buy_price)
                   VALUES (?, ?, 'Options', 'OPTION', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(portfolio_id, position_key) DO UPDATE SET
                     quantity = quantity + excluded.quantity,
                     avg_buy_price = ((avg_buy_price * quantity) + (excluded.avg_buy_price * excluded.quantity))
                                     / (quantity + excluded.quantity)""",
                (portfolio_id, ticker, position_key, option_type, strike, expiry, quantity, premium),
            )
            self.conn.execute(
                "UPDATE portfolios SET cash_balance = ROUND(cash_balance - ?, 2) WHERE id = ?",
                (trade_cost, portfolio_id),
            )
        else:
            if existing is None or existing["quantity"] < quantity:
                raise ValueError("Not enough option contracts to sell")
            remaining_qty = existing["quantity"] - quantity
            if remaining_qty <= 0:
                self.conn.execute("DELETE FROM holdings WHERE portfolio_id = ? AND position_key = ?", (portfolio_id, position_key))
            else:
                self.conn.execute(
                    "UPDATE holdings SET quantity = ? WHERE portfolio_id = ? AND position_key = ?",
                    (remaining_qty, portfolio_id, position_key),
                )
            self.conn.execute(
                "UPDATE portfolios SET cash_balance = ROUND(cash_balance + ?, 2) WHERE id = ?",
                (trade_cost, portfolio_id),
            )

        self.conn.execute(
            "INSERT INTO transactions (portfolio_id, ticker, instrument_type, option_type, strike, expiry, type, quantity, price, timestamp) VALUES (?, ?, 'OPTION', ?, ?, ?, ?, ?, ?, ?)",
            (portfolio_id, ticker, option_type, strike, expiry, side, quantity, premium, ts),
        )
        self.conn.commit()

        return {
            "portfolio_id": portfolio_id,
            "ticker": ticker,
            "instrument_type": "OPTION",
            "option_type": option_type,
            "strike": strike,
            "expiry": expiry,
            "quantity": quantity,
            "premium": round(float(premium), 2),
            "side": side,
            "timestamp": ts,
            "cash_balance": round(float(self.conn.execute("SELECT cash_balance FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()[0]), 2),
        }

    # -----------------------------------------------------------------
    # Mark-to-market: recompute portfolio value against current prices
    # -----------------------------------------------------------------

    @_locked
    def mark_to_market(self, portfolio_id, user_id, current_prices, as_of=None, current_option_prices=None):
        """current_prices: dict {ticker: price}; option values can also be supplied through current_option_prices keyed by position_key or ticker."""
        as_of = _normalize_timestamp(as_of)
        self._assert_owner(portfolio_id, user_id)

        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        holdings = self.conn.execute(
            "SELECT * FROM holdings WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchall()

        holdings_value = 0.0
        current_option_prices = current_option_prices or {}
        for h in holdings:
            if h["instrument_type"] == "OPTION":
                key = self._get_position_key(h["ticker"], "OPTION", h["option_type"], h["strike"], h["expiry"])
                option_price = current_option_prices.get(key)
                if option_price is None:
                    option_price = current_prices.get(h["ticker"])
                if option_price is None:
                    continue
                holdings_value += h["quantity"] * float(option_price)
                continue

            price = current_prices.get(h["ticker"])
            if price is None:
                continue
            holdings_value += h["quantity"] * float(price)

        total_cost_basis = 0.0
        for h in holdings:
            if h["quantity"] is None:
                continue
            total_cost_basis += float(h["quantity"]) * float(h["avg_buy_price"])

        total_value = holdings_value + portfolio["cash_balance"]
        total_contributed_capital = portfolio["cash_balance"] + total_cost_basis
        total_pl = total_value - total_contributed_capital
        total_pl_pct = (total_pl / total_contributed_capital) * 100 if total_contributed_capital else 0

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

    @_locked
    def add_cash_balance(self, portfolio_id, user_id, amount):
        if amount < 0:
            raise ValueError("Cash amount must be non-negative")
        self._assert_owner(portfolio_id, user_id)
        self.conn.execute(
            "UPDATE portfolios SET cash_balance = ROUND(cash_balance + ?, 2) WHERE id = ?",
            (amount, portfolio_id),
        )
        self.conn.commit()
        return self.get_portfolio_summary(portfolio_id, user_id)

    @_locked
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

        total_invested = 0.0
        sector_totals = {}
        unique_tickers = set()
        for holding in holdings:
            row = dict(holding)
            if row.get("instrument_type") == "OPTION":
                continue
            invested = float(row["quantity"]) * float(row["avg_buy_price"])
            total_invested += invested
            unique_tickers.add(row["ticker"])
            sector = row.get("sector") or "Unassigned"
            sector_totals[sector] = sector_totals.get(sector, 0.0) + invested

        cash_balance = float(portfolio["cash_balance"])
        total_holdings_value = float(latest_snapshot.get("holdings_value") or 0.0) if latest_snapshot else total_invested
        total_portfolio_value = total_holdings_value + cash_balance
        total_contributed_capital = total_invested + cash_balance
        total_pl = total_portfolio_value - total_contributed_capital
        total_pl_pct = (total_pl / total_contributed_capital) * 100 if total_contributed_capital else 0.0

        return {
            "name": portfolio["name"],
            "starting_capital": portfolio["starting_capital"],
            "cash_balance": round(cash_balance, 2),
            "remaining_balance_to_invest": round(cash_balance, 2),
            "total_invested": round(total_invested, 2),
            "total_portfolio_value": round(total_portfolio_value, 2),
            "total_pl": round(total_pl, 2),
            "total_pl_pct": round(total_pl_pct, 2),
            "sector_breakdown": {sector: round(value, 2) for sector, value in sorted(sector_totals.items())},
            "sector_count": len(sector_totals),
            "stock_count": len(unique_tickers),
            "holdings": [dict(h) for h in holdings],
            "latest": dict(latest_snapshot) if latest_snapshot else None,
            "holdings_value": round(total_holdings_value, 2),
        }

    @_locked
    def get_history(self, portfolio_id, user_id):
        self._assert_owner(portfolio_id, user_id)
        rows = self.conn.execute(
            "SELECT * FROM daily_snapshots WHERE portfolio_id = ? ORDER BY as_of ASC",
            (portfolio_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def get_trade_history(self, portfolio_id, user_id):
        self._assert_owner(portfolio_id, user_id)
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE portfolio_id = ? ORDER BY timestamp DESC",
            (portfolio_id,),
        ).fetchall()
        return [dict(r) for r in rows]
