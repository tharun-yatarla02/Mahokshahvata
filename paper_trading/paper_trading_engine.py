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
from decimal import ROUND_DOWN, ROUND_UP, Decimal


# ---------------------------------------------------------------------------
# Order rules for stock trades (paper trading, market orders only)
#   - Trades execute at the market price the caller supplies (the API passes
#     the latest tracked quote; users can't pick their own price).
#   - Fractional shares down to 6 decimals, so "$100 of AAPL" works.
#   - Money is in cents. Buys round the cost UP to the cent and sells round
#     the proceeds DOWN, so rounding can never create free money.
#   - Every order must be worth at least MIN_ORDER_VALUE; otherwise a tiny
#     fraction (0.000001 shares) would cost $0.00 after rounding.
# ---------------------------------------------------------------------------

SHARE_DECIMALS = 6
MIN_ORDER_VALUE = 1.00
_SHARE_STEP = Decimal(1).scaleb(-SHARE_DECIMALS)
_CENT = Decimal("0.01")


def round_shares(quantity):
    """Share count truncated to SHARE_DECIMALS (never rounds up)."""
    return float(Decimal(str(quantity)).quantize(_SHARE_STEP, rounding=ROUND_DOWN))


def order_value(quantity, price, side="BUY"):
    """Cash for an order in dollars: cost rounded up for buys, proceeds
    rounded down for sells."""
    rounding = ROUND_UP if side == "BUY" else ROUND_DOWN
    return float((Decimal(str(quantity)) * Decimal(str(price))).quantize(_CENT, rounding=rounding))


def shares_for_amount(amount, price):
    """How many shares a dollar amount buys at this price (whole cents never exceeded)."""
    quantity = round_shares(Decimal(str(amount)) / Decimal(str(price)))
    while quantity > 0 and order_value(quantity, price, "BUY") > amount:
        quantity = round_shares(quantity - float(_SHARE_STEP))
    return quantity


def format_shares(quantity):
    return f"{quantity:,.{SHARE_DECIMALS}f}".rstrip("0").rstrip(".")


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
    contributed_capital REAL,  -- starting capital + every deposit; P/L is measured against this
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
        self._backfill_contributed_capital()
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_holdings_portfolio_position ON holdings(portfolio_id, position_key)")

    def _backfill_contributed_capital(self):
        """Older databases didn't record deposits. Every cash movement other
        than a deposit is a trade, so: contributed = cash now + spent on buys
        - received from sells, summed over the transaction log."""
        columns = {col[1] for col in self.conn.execute("PRAGMA table_info(portfolios)").fetchall()}
        if "contributed_capital" not in columns:
            self.conn.execute("ALTER TABLE portfolios ADD COLUMN contributed_capital REAL")
        self.conn.execute(
            """UPDATE portfolios SET contributed_capital = ROUND(cash_balance + COALESCE((
                   SELECT SUM(CASE WHEN type = 'BUY' THEN quantity * price ELSE -quantity * price END)
                   FROM transactions WHERE transactions.portfolio_id = portfolios.id), 0), 2)
               WHERE contributed_capital IS NULL"""
        )

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

    DEFAULT_PORTFOLIO_NAME = "Demo Portfolio"
    DEFAULT_STARTING_CAPITAL = 100_000.0

    @_locked
    def get_or_create_default_portfolio(self, user_id):
        """The user's most recent portfolio, creating a $100,000 one on first
        use. Runs under the lock so two pages opening at once can't both
        create one."""
        row = self.conn.execute(
            "SELECT id FROM portfolios WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if row:
            return row["id"]
        return self.create_portfolio(user_id, self.DEFAULT_PORTFOLIO_NAME, self.DEFAULT_STARTING_CAPITAL)

    @_locked
    def create_portfolio(self, user_id, name, starting_capital):
        # Cash is kept rounded to cents at every write so float error can't
        # accumulate across trades (e.g. 0.1 + 0.2 != 0.3).
        # ponytail: REAL columns + rounding; switch to integer cents if exact
        # accounting (tax lots, fees, fractional shares) is ever needed.
        starting_capital = round(starting_capital, 2)
        cur = self.conn.execute(
            "INSERT INTO portfolios (user_id, name, starting_capital, cash_balance, contributed_capital, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, starting_capital, starting_capital, starting_capital, datetime.now(timezone.utc).isoformat()),
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
    def order_context(self, portfolio_id, user_id, ticker):
        """Cash available and shares of `ticker` held, for order previews."""
        self._assert_owner(portfolio_id, user_id)
        cash = self.conn.execute("SELECT cash_balance FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()[0]
        held = self.conn.execute(
            "SELECT quantity FROM holdings WHERE portfolio_id = ? AND position_key = ?",
            (portfolio_id, self._get_position_key((ticker or "").strip().upper(), instrument_type="STOCK")),
        ).fetchone()
        return {"cash": round(float(cash), 2), "owned": float(held[0]) if held else 0.0}

    @_locked
    def trade_stock(self, portfolio_id, user_id, ticker, quantity, price, side="BUY", timestamp=None, sector="Unknown"):
        """Market order for `quantity` shares (fractional allowed) at `price`,
        which must be the current market price. See the order rules above."""
        self._assert_owner(portfolio_id, user_id)
        ticker = (ticker or "").strip().upper()
        if not ticker:
            raise ValueError("Ticker is required")
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        trade_price = float(price)
        if trade_price <= 0:
            raise ValueError("Stock price must be positive")
        quantity = round_shares(quantity)
        if quantity <= 0:
            raise ValueError(f"Quantity must be at least {format_shares(float(_SHARE_STEP))} shares")

        ts = _normalize_timestamp(timestamp)
        portfolio = self.conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise ValueError(f"No portfolio with id {portfolio_id}")

        position_key = self._get_position_key(ticker, instrument_type="STOCK")
        existing = self.conn.execute(
            "SELECT * FROM holdings WHERE portfolio_id = ? AND position_key = ?",
            (portfolio_id, position_key),
        ).fetchone()
        if side == "SELL" and existing is not None and abs(existing["quantity"] - quantity) < float(_SHARE_STEP):
            quantity = float(existing["quantity"])  # "sell all" despite float noise

        trade_value = order_value(quantity, trade_price, side)
        selling_everything = side == "SELL" and existing is not None and quantity == float(existing["quantity"])
        if trade_value < MIN_ORDER_VALUE and not selling_everything:  # a small leftover position can always be sold
            raise ValueError(f"Minimum order is ${MIN_ORDER_VALUE:,.2f}; {format_shares(quantity)} {ticker} is ${trade_value:,.2f}")

        if side == "BUY":
            cash = float(portfolio["cash_balance"])
            if cash < trade_value:
                affordable = shares_for_amount(cash, trade_price)
                raise ValueError(
                    f"Not enough cash: {format_shares(quantity)} {ticker} at ${trade_price:,.2f} costs ${trade_value:,.2f}, "
                    f"but you have ${cash:,.2f}. That buys up to {format_shares(affordable)} shares."
                )
            self.conn.execute(
                """INSERT INTO holdings (portfolio_id, ticker, sector, instrument_type, position_key, quantity, avg_buy_price)
                   VALUES (?, ?, ?, 'STOCK', ?, ?, ?)
                   ON CONFLICT(portfolio_id, position_key) DO UPDATE SET
                     sector = CASE WHEN sector = 'Unknown' THEN excluded.sector ELSE sector END,
                     quantity = quantity + excluded.quantity,
                     avg_buy_price = ((avg_buy_price * quantity) + (excluded.avg_buy_price * excluded.quantity))
                                     / (quantity + excluded.quantity)""",
                (portfolio_id, ticker, sector or "Unknown", position_key, quantity, trade_price),
            )
            self.conn.execute(
                "UPDATE portfolios SET cash_balance = ROUND(cash_balance - ?, 2) WHERE id = ?",
                (trade_value, portfolio_id),
            )
        else:
            owned = float(existing["quantity"]) if existing else 0.0
            if quantity > owned:
                raise ValueError(f"You own {format_shares(owned)} {ticker}; can't sell {format_shares(quantity)}")
            remaining_qty = round_shares(owned - quantity)
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
        ticker = (ticker or "").strip().upper()
        if not ticker:
            raise ValueError("Ticker is required")
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
            else:
                option_price = None
            price = option_price if option_price is not None else current_prices.get(h["ticker"])
            # A holding with no quote keeps its cost basis rather than counting as $0.
            holdings_value += h["quantity"] * float(price if price is not None else h["avg_buy_price"])

        total_value = holdings_value + portfolio["cash_balance"]
        contributed = float(portfolio["contributed_capital"] or 0)
        total_pl = total_value - contributed
        total_pl_pct = (total_pl / contributed) * 100 if contributed else 0

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
    def fill_unknown_sectors(self, sector_for_ticker):
        """Sets the sector on stock holdings saved as 'Unknown' (manual trades
        used to always save that). sector_for_ticker(ticker) returns a sector
        name or None. Returns how many holdings were updated."""
        rows = self.conn.execute(
            "SELECT id, ticker FROM holdings WHERE instrument_type = 'STOCK' AND sector = 'Unknown'"
        ).fetchall()
        updated = 0
        for row in rows:
            sector = sector_for_ticker(row["ticker"])
            if sector and sector != "Unknown":
                self.conn.execute("UPDATE holdings SET sector = ? WHERE id = ?", (sector, row["id"]))
                updated += 1
        self.conn.commit()
        return updated

    @_locked
    def add_cash_balance(self, portfolio_id, user_id, amount):
        if amount < 0:
            raise ValueError("Cash amount must be non-negative")
        self._assert_owner(portfolio_id, user_id)
        self.conn.execute(
            """UPDATE portfolios SET cash_balance = ROUND(cash_balance + ?, 2),
                                     contributed_capital = ROUND(contributed_capital + ?, 2)
               WHERE id = ?""",
            (amount, amount, portfolio_id),
        )
        self.conn.commit()
        return self.get_portfolio_summary(portfolio_id, user_id)

    @_locked
    def get_portfolio_summary(self, portfolio_id, user_id, price_for=None):
        """price_for(ticker) -> latest price or None. Stocks are valued at that
        price (falling back to cost when there's no quote); options are valued
        at cost, since there's no options price feed. P/L is everything the
        portfolio is worth now minus everything put into it."""
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
        holdings_value = 0.0
        sector_totals = {}   # current market value per sector (options at cost)
        stock_sectors = set()
        unique_tickers = set()
        rows = []
        for holding in holdings:
            row = dict(holding)
            quantity = float(row["quantity"])
            cost = quantity * float(row["avg_buy_price"])
            price = None
            sector = row.get("sector") or "Unassigned"
            if row.get("instrument_type") != "OPTION":
                unique_tickers.add(row["ticker"])
                stock_sectors.add(sector)
                price = price_for(row["ticker"]) if price_for else None
            row["current_price"] = round(float(price), 2) if price else None
            row["market_value"] = round(quantity * float(price), 2) if price else round(cost, 2)
            total_invested += cost
            holdings_value += row["market_value"]
            sector_totals[sector] = sector_totals.get(sector, 0.0) + row["market_value"]
            rows.append(row)

        cash_balance = float(portfolio["cash_balance"])
        contributed = float(portfolio["contributed_capital"] or 0)
        total_portfolio_value = holdings_value + cash_balance
        total_pl = total_portfolio_value - contributed
        total_pl_pct = (total_pl / contributed) * 100 if contributed else 0.0

        return {
            "name": portfolio["name"],
            "starting_capital": portfolio["starting_capital"],
            "contributed_capital": round(contributed, 2),
            "cash_balance": round(cash_balance, 2),
            "remaining_balance_to_invest": round(cash_balance, 2),
            "total_invested": round(total_invested, 2),
            "total_portfolio_value": round(total_portfolio_value, 2),
            "total_pl": round(total_pl, 2),
            "total_pl_pct": round(total_pl_pct, 2),
            "sector_breakdown": {sector: round(value, 2) for sector, value in sorted(sector_totals.items())},
            "sector_count": len(stock_sectors),  # options aren't a sector
            "stock_count": len(unique_tickers),
            "holdings": rows,
            "latest": dict(latest_snapshot) if latest_snapshot else None,
            "holdings_value": round(holdings_value, 2),
        }

    @_locked
    def record_daily_snapshot(self, portfolio_id, summary, as_of=None):
        """Stores today's value from a summary, one row per portfolio per day
        (later calls the same day overwrite it). This is what the
        performance chart plots."""
        as_of = as_of or datetime.now(timezone.utc).date().isoformat()
        self.conn.execute(
            """INSERT INTO daily_snapshots (portfolio_id, as_of, total_value, cash_balance, holdings_value, total_pl, total_pl_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(portfolio_id, as_of) DO UPDATE SET
                 total_value = excluded.total_value,
                 cash_balance = excluded.cash_balance,
                 holdings_value = excluded.holdings_value,
                 total_pl = excluded.total_pl,
                 total_pl_pct = excluded.total_pl_pct""",
            (portfolio_id, as_of, summary["total_portfolio_value"], summary["cash_balance"],
             summary["holdings_value"], summary["total_pl"], summary["total_pl_pct"]),
        )
        self.conn.commit()

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
