"""
paper_trading_engine.py

A self-contained paper (simulated) trading engine. Builds a portfolio from
a ranked momentum stock list using simple risk/diversification rules,
tracks holdings, and marks the portfolio to market against price updates
over time — producing real profit/loss history without touching any real
brokerage account or real money.

Storage: Cloud Firestore (the Firebase project's database). Layout:

    users/{id}                         profile; password_hash for email accounts
    sessions/{sha256 of token}         one per signed-in browser
    auth_events/{auto}                 every register / login / logout
    counters/{users|portfolios}        last integer id handed out
    portfolios/{id}                    cash, capital, owner
    portfolios/{id}/holdings/{position_key}
    portfolios/{id}/transactions/{auto}
    portfolios/{id}/snapshots/{as_of}  one per day, for the performance chart

Ids stay integers (from the counters) so URLs and the API don't change.
Queries only ever filter on one field and sort in Python, so no composite
indexes are needed.

Usage as a library:
    from google.cloud import firestore
    from paper_trading_engine import PaperTradingEngine

    engine = PaperTradingEngine(firestore.Client())
    user_id = engine.get_or_create_user("some-firebase-uid")
    portfolio_id = engine.create_portfolio(user_id, "Momentum Demo", starting_capital=1000)
    engine.build_portfolio_from_momentum(portfolio_id, user_id, momentum_list)
    print(engine.get_portfolio_summary(portfolio_id, user_id))
"""

import functools
import secrets
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter


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



def _now():
    return datetime.now(timezone.utc).isoformat()


class PaperTradingEngine:
    def __init__(self, db=None):
        # Every public method takes self._lock (see _locked): reads and the
        # writes that depend on them (cash checks, then the trade) must not
        # interleave between FastAPI's worker threads. Multi-doc writes go in
        # one batch, so a trade lands completely or not at all.
        # ponytail: one lock per process; the deploy runs a single uvicorn
        # worker. Move trades into Firestore transactions for multi-process.
        self._lock = threading.RLock()
        self.db = db if db is not None else firestore.Client()

    def _portfolio_ref(self, portfolio_id):
        return self.db.collection("portfolios").document(str(portfolio_id))

    def _holding_ref(self, portfolio_id, position_key):
        return self._portfolio_ref(portfolio_id).collection("holdings").document(position_key)

    def _holdings(self, portfolio_id):
        return [h.to_dict() for h in self._portfolio_ref(portfolio_id).collection("holdings").stream()]

    def _get_holding(self, portfolio_id, position_key):
        snap = self._holding_ref(portfolio_id, position_key).get()
        return snap.to_dict() if snap.exists else None

    def _new_transaction(self, batch, portfolio_id, **fields):
        ref = self._portfolio_ref(portfolio_id).collection("transactions").document()
        batch.set(ref, {"portfolio_id": portfolio_id, "option_type": None, "strike": None, "expiry": None, **fields})

    def _next_id(self, kind):
        """Next integer id for users or portfolios, from counters/{kind}."""
        ref = self.db.collection("counters").document(kind)

        @firestore.transactional
        def bump(transaction):
            snap = ref.get(transaction=transaction)
            next_id = (snap.get("last") if snap.exists else 0) + 1
            transaction.set(ref, {"last": next_id})
            return next_id

        return bump(self.db.transaction())

    def _where(self, collection, field, value):
        return [d.to_dict() for d in self.db.collection(collection).where(filter=FieldFilter(field, "==", value)).stream()]

    # -----------------------------------------------------------------
    # Users
    # -----------------------------------------------------------------

    def _create_user(self, firebase_uid, email, name, password_hash=None):
        user_id = self._next_id("users")
        self.db.collection("users").document(str(user_id)).set({
            "id": user_id, "firebase_uid": firebase_uid, "email": email, "name": name,
            "password_hash": password_hash, "created_at": _now(),
        })
        return user_id

    def _local_email_taken(self, email, except_user_id=None):
        """Emails are unique among email/password accounts (stored lowercased)."""
        return any(u["password_hash"] and u["id"] != except_user_id for u in self._where("users", "email", email))

    @_locked
    def get_or_create_user(self, firebase_uid, email=None, name=None):
        """Looks up a user by their stable Firebase id, creating them on
        first sign-in. Returns the internal user_id (ours, not Firebase's)."""
        found = self._where("users", "firebase_uid", firebase_uid)
        if found:
            return found[0]["id"]
        return self._create_user(firebase_uid, email, name)

    @_locked
    def create_local_user(self, email, name, password_hash):
        """New email/password account. Raises ValueError if the email is taken.
        Local users get a random "local:" firebase_uid that can never collide
        with a real Firebase uid."""
        if self._local_email_taken(email):
            raise ValueError("An account with this email already exists")
        return self._create_user(f"local:{secrets.token_hex(12)}", email, name, password_hash)

    @_locked
    def get_local_login(self, email):
        """(user_id, password_hash) for a local account, or None."""
        for user in self._where("users", "email", email):
            if user["password_hash"]:
                return (user["id"], user["password_hash"])
        return None

    def _user_doc(self, user_id):
        snap = self.db.collection("users").document(str(user_id)).get()
        if not snap.exists:
            raise ValueError("User not found")
        return snap.to_dict()

    @_locked
    def get_user(self, user_id):
        user = self._user_doc(user_id)
        account = "email" if user["password_hash"] else "demo" if user["firebase_uid"] == "demo-token" else "google"
        return {"id": user["id"], "email": user["email"], "name": user["name"],
                "created_at": user["created_at"], "account_type": account}

    @_locked
    def update_user(self, user_id, name, email=None):
        """Changes the display name, and the email for local accounts."""
        changes = {"name": name}
        if email is not None:
            if self._local_email_taken(email, except_user_id=user_id):
                raise ValueError("An account with this email already exists")
            changes["email"] = email
        self.db.collection("users").document(str(user_id)).update(changes)

    @_locked
    def get_password_hash(self, user_id):
        try:
            return self._user_doc(user_id)["password_hash"]
        except ValueError:
            return None

    @_locked
    def set_password_hash(self, user_id, password_hash):
        self.db.collection("users").document(str(user_id)).update({"password_hash": password_hash})

    # -- sessions ------------------------------------------------------

    @_locked
    def create_session(self, user_id, token_hash, expires_at):
        self.db.collection("sessions").document(token_hash).set(
            {"user_id": user_id, "created_at": _now(), "expires_at": expires_at}
        )

    @_locked
    def get_session_user(self, token_hash):
        """user_id for a live session, or None if unknown or expired."""
        ref = self.db.collection("sessions").document(token_hash)
        snap = ref.get()
        if not snap.exists:
            return None
        if snap.get("expires_at") <= _now():
            ref.delete()
            return None
        return snap.get("user_id")

    @_locked
    def delete_session(self, token_hash):
        self.db.collection("sessions").document(token_hash).delete()

    @_locked
    def delete_other_sessions(self, user_id, keep_token_hash):
        """Signs out every other browser, e.g. after a password change."""
        batch = self.db.batch()
        for snap in self.db.collection("sessions").where(filter=FieldFilter("user_id", "==", user_id)).stream():
            if snap.id != keep_token_hash:
                batch.delete(snap.reference)
        batch.commit()

    def log_auth_event(self, event, user_id=None, method=None, email=None, ip=None, user_agent=None):
        """Appends a sign-in history row: event is register, login,
        login_failed or logout; method is email, google or demo."""
        self.db.collection("auth_events").add({
            "event": event, "user_id": user_id, "method": method, "email": email,
            "ip": ip, "user_agent": user_agent, "at": _now(),
        })

    # -----------------------------------------------------------------
    # Portfolios
    # -----------------------------------------------------------------

    def _assert_owner(self, portfolio_id, user_id):
        """Returns the portfolio, or raises PermissionError if it doesn't
        belong to this user. Every portfolio-touching method below calls this
        first — a user should never be able to view or modify someone else's
        simulated money."""
        snap = self._portfolio_ref(portfolio_id).get()
        if not snap.exists:
            raise ValueError(f"No portfolio with id {portfolio_id}")
        portfolio = snap.to_dict()
        if portfolio["user_id"] != user_id:
            raise PermissionError(f"Portfolio {portfolio_id} does not belong to this user")
        return portfolio

    def _portfolios_newest_first(self, user_id):
        return sorted(self._where("portfolios", "user_id", user_id), key=lambda p: (p["created_at"], p["id"]), reverse=True)

    @_locked
    def list_portfolios_for_user(self, user_id):
        keys = ("id", "name", "starting_capital", "cash_balance", "created_at")
        return [{k: p[k] for k in keys} for p in self._portfolios_newest_first(user_id)]

    DEFAULT_PORTFOLIO_NAME = "Demo Portfolio"
    DEFAULT_STARTING_CAPITAL = 100_000.0

    @_locked
    def get_or_create_default_portfolio(self, user_id):
        """The user's most recent portfolio, creating a $100,000 one on first
        use. Runs under the lock so two pages opening at once can't both
        create one."""
        existing = self._portfolios_newest_first(user_id)
        if existing:
            return existing[0]["id"]
        return self.create_portfolio(user_id, self.DEFAULT_PORTFOLIO_NAME, self.DEFAULT_STARTING_CAPITAL)

    @_locked
    def create_portfolio(self, user_id, name, starting_capital):
        # Cash is kept rounded to cents at every write so float error can't
        # accumulate across trades (e.g. 0.1 + 0.2 != 0.3).
        # ponytail: floats + rounding; switch to integer cents if exact
        # accounting (tax lots, fees, fractional shares) is ever needed.
        starting_capital = round(starting_capital, 2)
        portfolio_id = self._next_id("portfolios")
        self._portfolio_ref(portfolio_id).set({
            "id": portfolio_id, "user_id": user_id, "name": name, "starting_capital": starting_capital,
            "cash_balance": starting_capital, "contributed_capital": starting_capital, "created_at": _now(),
        })
        return portfolio_id

    # -----------------------------------------------------------------
    # Allocation logic: given a ranked momentum list and risk rules,
    # decide what to buy and how much.
    # -----------------------------------------------------------------

    def _get_position_key(self, ticker, instrument_type="STOCK", option_type=None, strike=None, expiry=None):
        if instrument_type == "OPTION":
            return _option_position_key(ticker, option_type, strike, expiry)
        return ticker

    def _add_to_holding(self, batch, portfolio_id, position_key, existing, quantity, price, new_fields):
        """Buys into a position: averages the cost into an existing holding,
        or creates it from new_fields."""
        if existing:
            total = existing["quantity"] + quantity
            avg = (existing["avg_buy_price"] * existing["quantity"] + price * quantity) / total
            batch.update(self._holding_ref(portfolio_id, position_key), {"quantity": total, "avg_buy_price": avg})
        else:
            batch.set(self._holding_ref(portfolio_id, position_key), {
                "portfolio_id": portfolio_id, "position_key": position_key, "option_type": None,
                "strike": None, "expiry": None, **new_fields, "quantity": quantity, "avg_buy_price": price,
            })

    def _reduce_holding(self, batch, portfolio_id, position_key, remaining_qty):
        ref = self._holding_ref(portfolio_id, position_key)
        if remaining_qty <= 0:
            batch.delete(ref)
        else:
            batch.update(ref, {"quantity": remaining_qty})

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
        portfolio = self._assert_owner(portfolio_id, user_id)

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

        # Execute the purchases: holdings + transactions + cash, in one batch
        batch = self.db.batch()
        for p in purchases:
            position_key = self._get_position_key(p["ticker"], instrument_type="STOCK")
            self._add_to_holding(batch, portfolio_id, position_key, self._get_holding(portfolio_id, position_key),
                                 p["quantity"], p["price"],
                                 {"ticker": p["ticker"], "sector": p["sector"], "instrument_type": "STOCK"})
            self._new_transaction(batch, portfolio_id, ticker=p["ticker"], instrument_type="STOCK", type="BUY",
                                  quantity=p["quantity"], price=p["price"], timestamp=as_of)

        new_cash = round(portfolio["cash_balance"] - sum(p["cost"] for p in purchases), 2)
        batch.update(self._portfolio_ref(portfolio_id), {"cash_balance": new_cash})
        batch.commit()
        return purchases

    @_locked
    def order_context(self, portfolio_id, user_id, ticker):
        """Cash available and shares of `ticker` held, for order previews."""
        portfolio = self._assert_owner(portfolio_id, user_id)
        held = self._get_holding(portfolio_id, self._get_position_key((ticker or "").strip().upper(), instrument_type="STOCK"))
        return {"cash": round(float(portfolio["cash_balance"]), 2), "owned": float(held["quantity"]) if held else 0.0}

    @_locked
    def trade_stock(self, portfolio_id, user_id, ticker, quantity, price, side="BUY", timestamp=None, sector="Unknown"):
        """Market order for `quantity` shares (fractional allowed) at `price`,
        which must be the current market price. See the order rules above."""
        portfolio = self._assert_owner(portfolio_id, user_id)
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
        position_key = self._get_position_key(ticker, instrument_type="STOCK")
        existing = self._get_holding(portfolio_id, position_key)
        if side == "SELL" and existing is not None and abs(existing["quantity"] - quantity) < float(_SHARE_STEP):
            quantity = float(existing["quantity"])  # "sell all" despite float noise

        trade_value = order_value(quantity, trade_price, side)
        selling_everything = side == "SELL" and existing is not None and quantity == float(existing["quantity"])
        if trade_value < MIN_ORDER_VALUE and not selling_everything:  # a small leftover position can always be sold
            raise ValueError(f"Minimum order is ${MIN_ORDER_VALUE:,.2f}; {format_shares(quantity)} {ticker} is ${trade_value:,.2f}")

        cash = float(portfolio["cash_balance"])
        batch = self.db.batch()
        if side == "BUY":
            if cash < trade_value:
                affordable = shares_for_amount(cash, trade_price)
                raise ValueError(
                    f"Not enough cash: {format_shares(quantity)} {ticker} at ${trade_price:,.2f} costs ${trade_value:,.2f}, "
                    f"but you have ${cash:,.2f}. That buys up to {format_shares(affordable)} shares."
                )
            self._add_to_holding(batch, portfolio_id, position_key, existing, quantity, trade_price,
                                 {"ticker": ticker, "sector": sector or "Unknown", "instrument_type": "STOCK"})
            if existing and existing["sector"] == "Unknown" and sector and sector != "Unknown":
                batch.update(self._holding_ref(portfolio_id, position_key), {"sector": sector})
            cash_after = round(cash - trade_value, 2)
        else:
            owned = float(existing["quantity"]) if existing else 0.0
            if quantity > owned:
                raise ValueError(f"You own {format_shares(owned)} {ticker}; can't sell {format_shares(quantity)}")
            self._reduce_holding(batch, portfolio_id, position_key, round_shares(owned - quantity))
            cash_after = round(cash + trade_value, 2)

        batch.update(self._portfolio_ref(portfolio_id), {"cash_balance": cash_after})
        self._new_transaction(batch, portfolio_id, ticker=ticker, instrument_type="STOCK", type=side,
                              quantity=quantity, price=trade_price, timestamp=ts)
        batch.commit()

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
        portfolio = self._assert_owner(portfolio_id, user_id)
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
        trade_cost = round(quantity * premium, 2)
        position_key = self._get_position_key(ticker, "OPTION", option_type, strike, expiry)
        existing = self._get_holding(portfolio_id, position_key)
        cash = float(portfolio["cash_balance"])

        batch = self.db.batch()
        if side == "BUY":
            if cash < trade_cost:
                raise ValueError("Insufficient cash to buy this option")
            self._add_to_holding(batch, portfolio_id, position_key, existing, quantity, premium, {
                "ticker": ticker, "sector": "Options", "instrument_type": "OPTION",
                "option_type": option_type, "strike": strike, "expiry": expiry,
            })
            cash_after = round(cash - trade_cost, 2)
        else:
            if existing is None or existing["quantity"] < quantity:
                raise ValueError("Not enough option contracts to sell")
            self._reduce_holding(batch, portfolio_id, position_key, existing["quantity"] - quantity)
            cash_after = round(cash + trade_cost, 2)

        batch.update(self._portfolio_ref(portfolio_id), {"cash_balance": cash_after})
        self._new_transaction(batch, portfolio_id, ticker=ticker, instrument_type="OPTION", option_type=option_type,
                              strike=strike, expiry=expiry, type=side, quantity=quantity, price=premium, timestamp=ts)
        batch.commit()

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
            "cash_balance": cash_after,
        }

    # -----------------------------------------------------------------
    # Mark-to-market: recompute portfolio value against current prices
    # -----------------------------------------------------------------

    def _save_snapshot(self, portfolio_id, as_of, total_value, cash_balance, holdings_value, total_pl, total_pl_pct):
        """One snapshot per portfolio per as_of; a later save overwrites it."""
        self._portfolio_ref(portfolio_id).collection("snapshots").document(as_of).set({
            "portfolio_id": portfolio_id, "as_of": as_of, "total_value": total_value,
            "cash_balance": cash_balance, "holdings_value": holdings_value,
            "total_pl": total_pl, "total_pl_pct": total_pl_pct,
        })

    def _snapshots(self, portfolio_id):
        rows = [s.to_dict() for s in self._portfolio_ref(portfolio_id).collection("snapshots").stream()]
        return sorted(rows, key=lambda s: s["as_of"])

    @_locked
    def mark_to_market(self, portfolio_id, user_id, current_prices, as_of=None, current_option_prices=None):
        """current_prices: dict {ticker: price}; option values can also be supplied through current_option_prices keyed by position_key or ticker."""
        as_of = _normalize_timestamp(as_of)
        portfolio = self._assert_owner(portfolio_id, user_id)

        holdings_value = 0.0
        current_option_prices = current_option_prices or {}
        for h in self._holdings(portfolio_id):
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

        self._save_snapshot(portfolio_id, as_of, total_value, portfolio["cash_balance"], holdings_value, total_pl, total_pl_pct)
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
        updated = 0
        for portfolio in self.db.collection("portfolios").stream():
            holdings = portfolio.reference.collection("holdings").where(filter=FieldFilter("sector", "==", "Unknown"))
            for snap in holdings.stream():
                if snap.get("instrument_type") != "STOCK":
                    continue
                sector = sector_for_ticker(snap.get("ticker"))
                if sector and sector != "Unknown":
                    snap.reference.update({"sector": sector})
                    updated += 1
        return updated

    @_locked
    def add_cash_balance(self, portfolio_id, user_id, amount):
        if amount < 0:
            raise ValueError("Cash amount must be non-negative")
        portfolio = self._assert_owner(portfolio_id, user_id)
        self._portfolio_ref(portfolio_id).update({
            "cash_balance": round(portfolio["cash_balance"] + amount, 2),
            "contributed_capital": round(portfolio["contributed_capital"] + amount, 2),
        })
        return self.get_portfolio_summary(portfolio_id, user_id)

    @_locked
    def get_portfolio_summary(self, portfolio_id, user_id, price_for=None):
        """price_for(ticker) -> latest price or None. Stocks are valued at that
        price (falling back to cost when there's no quote); options are valued
        at cost, since there's no options price feed. P/L is everything the
        portfolio is worth now minus everything put into it."""
        portfolio = self._assert_owner(portfolio_id, user_id)
        holdings = sorted(self._holdings(portfolio_id), key=lambda h: h["quantity"] * h["avg_buy_price"], reverse=True)
        snapshots = self._snapshots(portfolio_id)

        total_invested = 0.0
        holdings_value = 0.0
        sector_totals = {}   # current market value per sector (options at cost)
        stock_sectors = set()
        unique_tickers = set()
        rows = []
        for row in holdings:
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
            "latest": snapshots[-1] if snapshots else None,
            "holdings_value": round(holdings_value, 2),
        }

    @_locked
    def record_daily_snapshot(self, portfolio_id, summary, as_of=None):
        """Stores today's value from a summary, one per portfolio per day
        (later calls the same day overwrite it). This is what the
        performance chart plots."""
        as_of = as_of or datetime.now(timezone.utc).date().isoformat()
        self._save_snapshot(portfolio_id, as_of, summary["total_portfolio_value"], summary["cash_balance"],
                            summary["holdings_value"], summary["total_pl"], summary["total_pl_pct"])

    @_locked
    def get_history(self, portfolio_id, user_id):
        self._assert_owner(portfolio_id, user_id)
        return self._snapshots(portfolio_id)

    @_locked
    def get_trade_history(self, portfolio_id, user_id):
        self._assert_owner(portfolio_id, user_id)
        rows = [t.to_dict() for t in self._portfolio_ref(portfolio_id).collection("transactions").stream()]
        return sorted(rows, key=lambda t: t["timestamp"], reverse=True)
