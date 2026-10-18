"""
api/main.py

Backend API layer tying together the momentum engine, news sentiment
scraper, and paper trading engine — the "Backend API Layer" from the
architecture doc (step 9), the piece that connects data, analytics, AI
and portfolio services to a frontend.

Setup:
    pip install fastapi uvicorn yfinance pandas anthropic feedparser firebase-admin

Run:
    export GOOGLE_APPLICATION_CREDENTIALS="/path/to/firebase-service-account.json"
    uvicorn api.main:app --reload

Then visit http://localhost:8000/docs for interactive API docs (FastAPI
generates this automatically).

Auth: every /portfolio/* endpoint requires "Authorization: Bearer <Firebase
ID token>" — see auth/README.md for the Firebase project setup and the
frontend sign-in snippet. /momentum, /news-sentiment and the read-only /politicians endpoints don't
require sign-in; POST /politicians/refresh does.

Endpoints:
    GET  /momentum?top=10                     ranked momentum stocks
    GET  /explain/{ticker}                     plain-English explanation of one stock (sign-in)
    GET  /portfolio                            list the signed-in user's portfolios
    POST /portfolio                            create a portfolio
    POST /portfolio/{id}/build                 build it from a momentum list
    GET  /portfolio/{id}                       get portfolio summary
    POST /portfolio/{id}/mark-to-market         record today's value at live prices
"""

import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request, Path as FastAPIPath
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, BeforeValidator, Field, create_model, model_validator


def _load_dotenv(path=Path(__file__).parent.parent / ".env"):
    """Loads KEY=VALUE lines from the project's .env (gitignored) into the
    environment, so keys like ANTHROPIC_API_KEY work however the server is
    started. Variables already set in the environment win."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Before the project imports: some modules read their settings at import time.
_load_dotenv()

from momentum.momentum_engine import latest_price, fetch_price_history, filter_by_group, search_rows, DEFAULT_SECTOR_LOOKUP
from momentum.market_data import MarketData
from paper_trading.paper_trading_engine import (
    MIN_ORDER_VALUE, PaperTradingEngine, RiskRules, format_shares, order_value, round_shares, shares_for_amount,
)
from politician_trades.politician_trades import list_politicians, get_trades_for_politician, refresh_cache
from auth.firebase_auth import verify_firebase_token
from auth.passwords import SESSION_DAYS, hash_password, new_session_token, token_hash, verify_password
from auth.rate_limit import LoginRateLimiter
from news_sentiment.news_sentiment_scraper import Anthropic, collect_sentiment_results
from news_sentiment.suggestions import affected_stocks, build_suggestions, company_aliases, largest_by_sector, news_by_ticker
from fundamentals.sec_edgar import get_fundamentals
from explain.stock_explainer import build_facts, explain_with_claude, explain_with_rules
from autopilot.autopilot import SETTINGS as AUTOPILOT_SETTINGS, clamp_settings, plan_run
from momentum.market_calendar import market_status, session as market_session

@asynccontextmanager
async def lifespan(app):
    # Loads the saved market snapshot, then keeps quotes and history fresh in
    # a background thread (see momentum/market_data.py).
    market_data.start()
    engine.fill_unknown_sectors(lambda ticker: (market_data.lookup(ticker) or {}).get("sector"))
    if os.environ.get("AUTOPILOT_SCHEDULER", "on").lower() != "off":
        threading.Thread(target=_autopilot_scheduler, name="autopilot", daemon=True).start()
    yield


app = FastAPI(title="Mahokshahvata API", version="0.1.0", lifespan=lifespan)
frontend_dir = Path(__file__).parent.parent / "frontend"
engine = PaperTradingEngine()  # Firestore: GOOGLE_APPLICATION_CREDENTIALS picks the project
market_data = MarketData()

# Portfolio builds pick from the largest names only, so a momentum build
# doesn't fill up on small caps that happened to triple this quarter.
BUILD_UNIVERSE_SIZE = 500

# CORS: without this, a browser blocks every call from your frontend to this
# API once they're on different origins (different port counts as different
# origin too — localhost:5500 calling localhost:8000 is already cross-origin).
# ALLOWED_ORIGINS is a comma-separated env var, e.g.
#   export ALLOWED_ORIGINS="https://your-dashboard.com,http://localhost:5500"
# Defaults to local development origins only. The bundled frontend is served
# by this app (same origin), so it doesn't need CORS at all. Set this to your
# real frontend's exact domain(s) if you host it elsewhere; "*" allows any
# website to call the API and should only be used for throwaway testing.
_DEFAULT_ORIGINS = (
    "http://localhost:8000,http://127.0.0.1:8000,"
    "http://localhost:5500,http://127.0.0.1:5500"
)
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", _DEFAULT_ORIGINS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allowed_origins == "*" else [o.strip() for o in _allowed_origins.split(",") if o.strip()],
    allow_credentials=False,  # we use a Bearer token, not cookies, so this can stay False
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # FastAPI's default 422 echoes the rejected input back, and a rejected
    # Infinity/NaN can't be written as JSON, so the error itself would 500.
    errors = [{"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


# ---------------------------------------------------------------------------
# Auth dependency — every portfolio endpoint requires a valid session token
# ---------------------------------------------------------------------------

def _bearer_token(authorization):
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip() or None
    return None


def get_current_user(authorization: str = Header(None)):
    """Reads 'Authorization: Bearer <token>' and returns our internal user_id.
    Raises 401 if the token is missing, unknown or expired. FastAPI's
    Depends() runs this before the endpoint body, so an endpoint that declares
    it can assume the caller is signed in.

    The token is checked in this order:
      1. a session from email/password sign-in (/auth/login), stored hashed
         in the sessions table;
      2. "demo-token", only when USE_DEMO_AUTH=true;
      3. a Firebase ID token (Google sign-in), when Firebase is configured.
    A request with no token is always rejected, even in demo mode, so a
    signed-out browser can't see anyone's portfolio."""
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Please sign in")

    user_id = engine.get_session_user(token_hash(token))
    if user_id is not None:
        return user_id

    try:
        firebase_user = verify_firebase_token(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")

    try:
        return engine.get_or_create_user(
            firebase_uid=firebase_user["firebase_uid"],
            email=firebase_user["email"],
            name=firebase_user["name"],
            email_verified=firebase_user.get("email_verified", False),
        )
    except ValueError as e:  # email already used by another account
        raise HTTPException(status_code=409, detail=str(e))


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

# Money fields reject Infinity/NaN (Python's JSON parser accepts them, and an
# infinite cash balance can't be serialized back out, which breaks the
# portfolio for good) and cap at $1B so a typo can't do the same.
MAX_AMOUNT = 1_000_000_000
TICKER_PATTERN = r"^\s*[A-Za-z]{1,5}([.-][A-Za-z]{1,2})?\s*$"


def _money(**kwargs):
    return Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False, **kwargs)


def _normalize_ticker(ticker):
    # BRK.B and BRK-B are the same stock; the market data spells it BRK-B.
    return ticker.strip().upper().replace(".", "-")


class CreatePortfolioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    starting_capital: float = _money(description="Must be a positive amount")


class BuildPortfolioRequest(BaseModel):
    max_positions: int = Field(default=5, gt=0, le=20)
    max_allocation_per_stock: float = Field(default=0.30, gt=0, le=1.0)
    max_allocation_per_sector: float = Field(default=0.50, gt=0, le=1.0)


class AddCashRequest(BaseModel):
    amount: float = _money(description="Must be a positive amount")


class TradeStockRequest(BaseModel):
    """A market order: give either `quantity` (shares, fractions allowed) or
    `amount` (dollars to invest or to sell). It always executes at the
    current market price; `price` is accepted from older clients but ignored."""
    ticker: str = Field(pattern=TICKER_PATTERN)
    quantity: float | None = Field(default=None, gt=0, le=10_000_000, allow_inf_nan=False)
    amount: float | None = _money(default=None)
    price: float | None = None
    side: str = Field(default="BUY", pattern="^(BUY|SELL)$")

    @model_validator(mode="after")
    def quantity_or_amount(self):
        if (self.quantity is None) == (self.amount is None):
            raise ValueError("Give either quantity (shares) or amount (dollars)")
        return self


class TradeOptionRequest(BaseModel):
    ticker: str = Field(pattern=TICKER_PATTERN)
    option_type: str = Field(..., pattern="^(CALL|PUT)$")
    strike: float = _money()
    expiry: date  # YYYY-MM-DD
    quantity: int = Field(gt=0, le=10_000_000)
    premium: float = _money()
    side: str = Field(default="BUY", pattern="^(BUY|SELL)$")


# ---------------------------------------------------------------------------
# Accounts: email/password sign-in, profile, sign-out
# ---------------------------------------------------------------------------

# Deliberately loose: one "@", something on each side, a dot in the domain.
# Real validation is the user receiving mail, which this app doesn't send.
EMAIL_PATTERN = r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$"


# Emails are trimmed before validation (pasted addresses often carry spaces);
# passwords are never trimmed.
Email = Annotated[str, BeforeValidator(lambda v: v.strip() if isinstance(v, str) else v),
                  Field(max_length=254, pattern=EMAIL_PATTERN)]


def _new_password():
    return Field(min_length=8, max_length=128, description="8 to 128 characters")


def _clean_name(name):
    name = " ".join(name.split())
    if not name:
        raise HTTPException(status_code=422, detail="Name can't be blank")
    return name


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: Email
    password: str = _new_password()


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class UpdateProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: Email | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = _new_password()


class GoogleLoginRequest(BaseModel):
    id_token: str = Field(min_length=1, max_length=4096)


def _client_ip(request):
    """Caddy replaces X-Forwarded-For with the real client address, so it can't
    be spoofed from outside; without Caddy (local runs) the socket address is used."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else None)


def _log_auth(request, event, user_id=None, method=None, email=None):
    """Records a sign-in history row in Firestore (auth_events)."""
    engine.log_auth_event(event, user_id=user_id, method=method, email=email,
                          ip=_client_ip(request), user_agent=request.headers.get("user-agent"))


def _start_session(user_id):
    token = new_session_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    engine.create_session(user_id, token_hash(token), expires_at)
    return {"token": token, "expires_at": expires_at, "user": engine.get_user(user_id)}


@app.post("/auth/register", status_code=201)
def register(req: RegisterRequest, request: Request):
    """Creates an email/password account with its default $100,000
    simulated portfolio, and signs it in."""
    email = req.email.strip().lower()
    try:
        user_id = engine.create_local_user(email, _clean_name(req.name), hash_password(req.password))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    engine.get_or_create_default_portfolio(user_id)
    _log_auth(request, "register", user_id, "email", email)
    return _start_session(user_id)


login_limiter = LoginRateLimiter()


@app.post("/auth/login")
def login(req: LoginRequest, request: Request):
    """Signs in with email and password. The same message for an unknown
    email and a wrong password. (Registration still reveals whether an email
    is taken, via 409, as most sign-up forms do.) Repeated failures for one
    email or from one IP are refused with 429 for a while (auth/rate_limit.py)."""
    email = req.email.strip().lower()
    ip = _client_ip(request)
    wait = login_limiter.retry_after(email, ip)
    if wait:
        _log_auth(request, "login_throttled", method="email", email=email)
        minutes = -(-wait // 60)
        raise HTTPException(status_code=429, headers={"Retry-After": str(wait)},
                            detail=f"Too many sign-in attempts. Try again in {minutes} minute{'s' if minutes > 1 else ''}.")
    found = engine.get_local_login(email)
    if found and found[1] is None:  # one account per email, and this one uses Google
        _log_auth(request, "login_failed", found[0], "email", email)
        raise HTTPException(status_code=400, detail="This account signs in with Google. Use Continue with Google.")
    if not found or not verify_password(req.password, found[1]):
        login_limiter.record_failure(email, ip)
        _log_auth(request, "login_failed", found[0] if found else None, "email", email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    login_limiter.record_success(email)
    _log_auth(request, "login", found[0], "email", email)
    return _start_session(found[0])


@app.post("/auth/google")
def google_login(req: GoogleLoginRequest, request: Request):
    """Trades a Firebase ID token from the Google sign-in popup for one of our
    sessions. Firebase ID tokens expire after an hour; our session lasts
    SESSION_DAYS and works like an email/password one from here on. An email
    that already has an account signs into it (see get_or_create_user)."""
    try:
        firebase_user = verify_firebase_token(req.id_token)
    except ValueError:
        _log_auth(request, "login_failed", method="google")
        raise HTTPException(status_code=401, detail="Google sign-in failed. Please try again.")
    try:
        user_id = engine.get_or_create_user(firebase_user["firebase_uid"], firebase_user["email"],
                                            firebase_user["name"], firebase_user.get("email_verified", False))
    except ValueError as e:  # email taken and Google hasn't verified it
        _log_auth(request, "login_failed", method="google", email=firebase_user["email"])
        raise HTTPException(status_code=409, detail=str(e))
    _log_auth(request, "login", user_id, "google", firebase_user["email"])
    return _start_session(user_id)


@app.post("/auth/logout")
def logout(request: Request, authorization: str = Header(None)):
    """Ends this browser's session. Always succeeds, so a stale token can
    still sign out cleanly."""
    token = _bearer_token(authorization)
    if token:
        user_id = engine.get_session_user(token_hash(token))
        engine.delete_session(token_hash(token))
        if user_id is not None:
            _log_auth(request, "logout", user_id)
    return {"signed_out": True}


@app.get("/auth/me")
def get_me(user_id: int = Depends(get_current_user)):
    return engine.get_user(user_id)


@app.patch("/auth/me")
def update_me(req: UpdateProfileRequest, user_id: int = Depends(get_current_user)):
    """Updates the display name, and the email for email/password accounts
    (Google and demo accounts get their email from the sign-in provider)."""
    user = engine.get_user(user_id)
    email = None
    if req.email is not None and req.email.strip().lower() != (user["email"] or ""):
        if user["account_type"] != "email":
            raise HTTPException(status_code=400, detail="This account's email comes from its sign-in provider")
        email = req.email.strip().lower()
    try:
        engine.update_user(user_id, _clean_name(req.name), email)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return engine.get_user(user_id)


@app.post("/auth/password")
def change_password(req: ChangePasswordRequest, authorization: str = Header(None),
                    user_id: int = Depends(get_current_user)):
    """Changes the password and signs out every other browser."""
    current = engine.get_password_hash(user_id)
    if not current:
        raise HTTPException(status_code=400, detail="This account signs in without a password")
    if not verify_password(req.current_password, current):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    engine.set_password_hash(user_id, hash_password(req.new_password))
    engine.delete_other_sessions(user_id, token_hash(_bearer_token(authorization)))
    return {"changed": True}


# ---------------------------------------------------------------------------
# Momentum endpoints
# ---------------------------------------------------------------------------

_SORT_KEYS = {
    "momentum": lambda r: r["momentum_pct"],
    "market_cap": lambda r: r.get("market_cap") or 0,
    "day_change": lambda r: r.get("day_change_pct") or 0,
    "volume": lambda r: r.get("volume") or 0,
}


@app.get("/momentum")
def get_momentum(
    top: int = Query(10, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    sector: str | None = None,
    q: str | None = None,
    sort: str = Query("momentum", pattern="^(momentum|market_cap|day_change|volume)$"),
):
    """Ranked momentum for the tracked US stocks (top 3000 by market cap),
    served from the background snapshot in momentum/market_data.py.
    No sign-in required — momentum data isn't tied to any one user.

    `sector` accepts a sector name ("Energy"), a short alias ("Tech") or a
    theme ("AI", "Growth") — see THEMES in momentum_engine.py.
    `q` searches ticker, company name and sector, best match first.
    `sort` orders results (highest first) when there's no search; `top`
    and `offset` page through them. `total` is the full match count."""
    rows = market_data.snapshot()
    if sector:
        rows = filter_by_group(rows, sector)
    if q and q.strip():
        rows = search_rows(rows, q)
    elif sort != "momentum":  # the snapshot is already in momentum order
        rows = sorted(rows, key=_SORT_KEYS[sort], reverse=True)
    page = rows[offset:offset + top]
    return {
        "count": len(page),
        "total": len(rows),
        "offset": offset,
        "results": page,
        "sectors": market_data.sectors(),
        "status": market_data.status(),
        "market": _market_view(),
    }


# ---------------------------------------------------------------------------
# Portfolio (paper trading) endpoints — every one requires sign-in, and every
# one is scoped to the signed-in user's own portfolios only.
# ---------------------------------------------------------------------------

@app.get("/portfolio")
def list_my_portfolios(user_id: int = Depends(get_current_user)):
    return {"portfolios": engine.list_portfolios_for_user(user_id)}


@app.post("/portfolio/default")
def default_portfolio(user_id: int = Depends(get_current_user)):
    """The portfolio the dashboard pages work with: the user's most recent
    one, created with $100,000 on first visit."""
    return {"portfolio_id": engine.get_or_create_default_portfolio(user_id)}


@app.post("/portfolio")
def create_portfolio(req: CreatePortfolioRequest, user_id: int = Depends(get_current_user)):
    portfolio_id = engine.create_portfolio(user_id, req.name, req.starting_capital)
    return {"portfolio_id": portfolio_id}


@app.post("/portfolio/{portfolio_id}/build")
def build_portfolio(portfolio_id: int, req: BuildPortfolioRequest, user_id: int = Depends(get_current_user)):
    """Builds the portfolio from the current momentum ranking. Only works
    once per portfolio — guards against a double-click or retry silently
    buying twice and double-spending the portfolio's cash."""
    try:
        existing = engine.get_portfolio_summary(portfolio_id, user_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if existing["holdings"]:
        raise HTTPException(
            status_code=409,
            detail="This portfolio has already been built. Create a new portfolio to try a different allocation.",
        )

    largest = sorted(market_data.snapshot(), key=_SORT_KEYS["market_cap"], reverse=True)[:BUILD_UNIVERSE_SIZE]
    momentum_list = sorted(largest, key=_SORT_KEYS["momentum"], reverse=True)
    if not momentum_list:
        raise HTTPException(status_code=503, detail="Market data is still loading. Try again in a couple of minutes.")

    # momentum_engine.py outputs "current_price"; paper_trading_engine.py
    # expects "price" — this adapts between the two, since without it every
    # real (non-hand-written) momentum list crashes the allocation engine
    # with a KeyError the moment it tries to read the price.
    normalized_momentum_list = [
        {**stock, "price": stock["current_price"]} for stock in momentum_list
    ]

    rules = RiskRules(
        max_positions=req.max_positions,
        max_allocation_per_stock=req.max_allocation_per_stock,
        max_allocation_per_sector=req.max_allocation_per_sector,
    )
    try:
        purchases = engine.build_portfolio_from_momentum(portfolio_id, user_id, normalized_momentum_list, rules)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"portfolio_id": portfolio_id, "purchases": purchases}


def _live_price(ticker):
    return (market_data.lookup(ticker) or {}).get("current_price")


@app.get("/portfolio/{portfolio_id}")
def get_portfolio(portfolio_id: int, user_id: int = Depends(get_current_user)):
    """Returns the portfolio's current state, with stocks valued at the latest
    tracked price. A freshly created portfolio with no holdings yet is a
    normal, valid response — not a 404. Only a portfolio that truly doesn't
    exist (or belongs to someone else) errors."""
    try:
        summary = engine.get_portfolio_summary(portfolio_id, user_id, price_for=_live_price)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if market_data.status()["tracked"]:  # only record values backed by live prices
        engine.record_daily_snapshot(portfolio_id, summary)
    return summary


@app.post("/portfolio/{portfolio_id}/mark-to-market")
def mark_to_market(portfolio_id: int, user_id: int = Depends(get_current_user)):
    """Records today's value at the tracked prices. It used to take prices
    from the caller, which let anyone write made-up values into the
    performance history that the autopilot's daily loss limit reads."""
    try:
        summary = engine.get_portfolio_summary(portfolio_id, user_id, price_for=_live_price)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if not market_data.status()["tracked"]:
        raise HTTPException(status_code=503, detail="Market data is still loading. Try again in a couple of minutes.")
    engine.record_daily_snapshot(portfolio_id, summary)
    return {key: summary[key] for key in ("total_portfolio_value", "cash_balance", "holdings_value", "total_pl", "total_pl_pct")}


@app.post("/portfolio/{portfolio_id}/cash")
def add_cash_to_portfolio(portfolio_id: int, req: AddCashRequest, user_id: int = Depends(get_current_user)):
    try:
        engine.add_cash_balance(portfolio_id, user_id, req.amount)
        return engine.get_portfolio_summary(portfolio_id, user_id, price_for=_live_price)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


FALLBACK_PRICE_TTL_SECONDS = 60
_fallback_prices = {}  # ticker -> (monotonic time, price or None)


def _market_price(ticker):
    """(price, source) for a market order: the tracked quote the dashboard
    shows, or for untracked tickers a yfinance lookup, cached briefly so a
    live order preview doesn't download on every keystroke."""
    tracked = market_data.lookup(ticker)
    if tracked:
        return tracked["current_price"], "live quote (~15 min delayed)"
    cached = _fallback_prices.get(ticker)
    if cached and time.monotonic() - cached[0] < FALLBACK_PRICE_TTL_SECONDS:
        price = cached[1]
    else:
        price = latest_price(ticker)
        _fallback_prices[ticker] = (time.monotonic(), price)
    return (round(price, 2), "latest close") if price else (None, None)


def _plan_order(portfolio_id, user_id, req):
    """Works out a market order without placing it: shares, value, and
    whether cash or holdings cover it. Shared by the preview and the trade,
    so the preview always matches what the trade will do."""
    ticker = _normalize_ticker(req.ticker)
    price, source = _market_price(ticker)
    if price is None:
        raise HTTPException(status_code=404, detail=f"No market price found for {ticker}. Check the ticker symbol.")
    try:
        context = engine.order_context(portfolio_id, user_id, ticker)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cash, owned = context["cash"], context["owned"]
    if req.amount is not None:
        quantity = shares_for_amount(req.amount, price) if req.side == "BUY" else round_shares(req.amount / price)
    else:
        quantity = round_shares(req.quantity)
    value = order_value(quantity, price, req.side)
    max_buy = shares_for_amount(cash, price)

    selling_everything = req.side == "SELL" and owned > 0 and abs(quantity - owned) < 1e-6
    if selling_everything:
        quantity, value = owned, order_value(owned, price, "SELL")
    problem = None
    if quantity <= 0 or (value < MIN_ORDER_VALUE and not selling_everything):
        problem = f"Minimum order is ${MIN_ORDER_VALUE:,.2f}"
    elif req.side == "BUY" and value > cash:
        problem = (f"Not enough cash: {format_shares(quantity)} {ticker} costs ${value:,.2f}, "
                   f"you have ${cash:,.2f}. That buys up to {format_shares(max_buy)} shares.")
    elif req.side == "SELL" and owned == 0:
        problem = f"You don't own any {ticker}."
    elif req.side == "SELL" and quantity > owned + 1e-9:
        problem = f"You own {format_shares(owned)} {ticker}; can't sell {format_shares(quantity)}."

    cash_after = cash - value if req.side == "BUY" else cash + value
    market = _market_view()
    return {
        "ticker": ticker,
        "side": req.side,
        "market_price": price,
        "price_source": source,
        "quantity": quantity,
        "value": value,
        "cash_balance": cash,
        "cash_after": round(cash_after, 2),
        "shares_owned": owned,
        "max_buy_quantity": max_buy,
        "max_buy_whole_shares": int(max_buy),
        "ok": problem is None,
        # Paper orders are allowed while the market is closed; they fill at the last close.
        "market_open": market["open"],
        "market_note": None if market["open"] else f"{market['label']}. This order fills at the last closing price.",
        "message": problem or (
            f"{'Buy' if req.side == 'BUY' else 'Sell'} {format_shares(quantity)} {ticker} at ${price:,.2f} "
            f"for ${value:,.2f}"
        ),
    }


@app.post("/portfolio/{portfolio_id}/trade-preview")
def preview_trade(portfolio_id: int, req: TradeStockRequest, user_id: int = Depends(get_current_user)):
    """What a market order would do right now, without placing it."""
    return _plan_order(portfolio_id, user_id, req)


@app.post("/portfolio/{portfolio_id}/trade-stock")
def trade_stock(portfolio_id: int, req: TradeStockRequest, user_id: int = Depends(get_current_user)):
    """Places a market order at the current market price (see TradeStockRequest)."""
    plan = _plan_order(portfolio_id, user_id, req)
    if not plan["ok"]:
        raise HTTPException(status_code=400, detail=plan["message"])
    sector = ((market_data.lookup(plan["ticker"]) or {}).get("sector")
              or DEFAULT_SECTOR_LOOKUP.get(plan["ticker"], "Unknown"))
    try:
        result = engine.trade_stock(
            portfolio_id,
            user_id,
            plan["ticker"],
            plan["quantity"],
            plan["market_price"],
            req.side,
            sector=sector,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise _trade_error(e)
    return {**result, "value": plan["value"], "price_source": plan["price_source"], "market_note": plan["market_note"]}


def _trade_error(e):
    # The engine raises ValueError both for a missing portfolio and for a
    # rejected trade (insufficient cash, not enough to sell, ...).
    status = 404 if str(e).startswith("No portfolio") else 400
    return HTTPException(status_code=status, detail=str(e))


@app.post("/portfolio/{portfolio_id}/trade-option")
def trade_option(portfolio_id: int, req: TradeOptionRequest, user_id: int = Depends(get_current_user)):
    if req.side == "BUY" and req.expiry < date.today():
        raise HTTPException(status_code=400, detail=f"Option expired on {req.expiry.isoformat()}")
    ticker = _normalize_ticker(req.ticker)
    underlying, _ = _market_price(ticker)  # bounds the hand-typed premium
    if underlying is None:
        raise HTTPException(status_code=404, detail=f"No market price found for {ticker}. Check the ticker symbol.")
    try:
        return engine.trade_option(
            portfolio_id,
            user_id,
            ticker,
            req.option_type,
            req.strike,
            req.expiry.isoformat(),
            req.quantity,
            req.premium,
            req.side,
            underlying_price=underlying,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise _trade_error(e)


# ---------------------------------------------------------------------------
# Autopilot: paper-trades a portfolio by rules (see autopilot/autopilot.py)
# ---------------------------------------------------------------------------

AUTOPILOT_CHECK_SECONDS = 10 * 60
MARKET_TZ = ZoneInfo("America/New_York")
AUTOPILOT_START = clock_time(10, 0)             # let the open settle first
AUTOPILOT_STOP_BEFORE_CLOSE = timedelta(minutes=30)
CLOSE_SNAPSHOT_DELAY = timedelta(minutes=20)    # quotes are ~15 min delayed
_autopilot_lock = threading.Lock()  # one run at a time (manual or scheduled)

AutopilotUpdate = create_model(
    "AutopilotUpdate",
    enabled=(bool | None, None),
    use_news=(bool | None, None),
    **{key: (int | None, Field(None, ge=low, le=high)) for key, (_, low, high) in AUTOPILOT_SETTINGS.items()},
)


def _portfolio_errors(fn):
    try:
        return fn()
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _autopilot_view(portfolio_id, user_id):
    state = engine.get_autopilot(portfolio_id, user_id)
    return {
        "enabled": bool(state.get("enabled")),
        "paused_reason": state.get("paused_reason"),
        "settings": clamp_settings(state.get("settings")),
        "limits": {key: {"min": low, "max": high} for key, (_, low, high) in AUTOPILOT_SETTINGS.items()},
        "managed": state.get("managed", []),
        "last_run_at": state.get("last_run_at"),
        "runs": engine.list_autopilot_runs(portfolio_id, user_id),
        "market": _market_view(),
    }


def _market_view():
    """market_status() plus plain-language text for the page."""
    status = market_status()
    if status["open"]:
        closes = datetime.fromisoformat(status["closes_at"])
        status["label"] = f"Market open until {closes:%-H:%M} New York time"
    else:
        opens = datetime.fromisoformat(status["next_open"])
        status["label"] = f"Market closed ({status['reason']}). Next open: {opens:%a %b %-d, %-H:%M} New York time"
    return status


def _autopilot_news(rows):
    """{ticker: tone and verdict} from the current headlines, or None when
    there's no real news (feeds down, or only the demo fallback)."""
    try:
        data, _ = _sentiment_data(rows)
    except Exception as e:
        print(f"[warn] news unavailable for autopilot: {type(e).__name__}", file=sys.stderr)
        return None
    return None if data.get("fallback") else news_by_ticker(data["results"], market_data.lookup)


def _run_autopilot(portfolio_id, user_id, execute, trigger):
    """Plans a run and, when execute is set, places its trades and logs it.
    Trades fill at the same tracked quotes as manual market orders."""
    with _autopilot_lock:
        state = engine.get_autopilot(portfolio_id, user_id)
        market = _market_view()
        if execute and not market["open"]:
            return {"status": "closed", "trades": [], "notes": [market["label"] + ". Nothing was traded."]}
        status = market_data.status()
        if status["warming_up"] or status["stale"]:
            return {"status": "idle", "trades": [], "retry": True,
                    "notes": ["Market prices are still loading or out of date; try again shortly."]}
        rows = market_data.snapshot()
        summary = engine.get_portfolio_summary(portfolio_id, user_id, price_for=_live_price)
        holdings = [h for h in summary["holdings"] if h.get("instrument_type") != "OPTION"]
        settings = clamp_settings(state.get("settings"))
        universe = sorted(rows, key=lambda r: r.get("market_cap") or 0, reverse=True)[:BUILD_UNIVERSE_SIZE]
        today = datetime.now(MARKET_TZ).date().isoformat()
        plan = plan_run(settings, summary["cash_balance"], summary["total_portfolio_value"], holdings,
                        state.get("managed", []), market_data.lookup, universe,
                        _autopilot_news(rows) if settings["use_news"] else {},
                        engine.last_close_value(portfolio_id, today))
        if not execute:
            if not market["open"]:
                plan["notes"].insert(0, market["label"] + ". This preview uses the last closing prices; "
                                     "trades can only be placed while the market is open.")
            return plan

        managed = [t for t in state.get("managed", []) if any(h["ticker"] == t for h in holdings)]
        for trade in plan["trades"]:
            try:
                engine.trade_stock(portfolio_id, user_id, trade["ticker"], trade["quantity"], trade["price"],
                                   trade["side"], sector=trade.get("sector") or "Unknown", source="autopilot")
                trade["result"] = "done"
                if trade["side"] == "BUY":
                    managed.append(trade["ticker"])
                elif trade["ticker"] in managed:
                    managed.remove(trade["ticker"])
            except ValueError as e:  # cash moved since the plan, etc.
                trade["result"] = f"skipped: {e}"
        now = datetime.now(timezone.utc).isoformat()
        changes = {"managed": managed, "last_run_at": now}
        if plan["status"] == "paused":
            changes.update(enabled=False, paused_reason=plan["notes"][-1])
        engine.update_autopilot(portfolio_id, user_id, changes)
        engine.save_autopilot_run(portfolio_id, {"at": now, "trigger": trigger, "status": plan["status"],
                                                 "value_before": summary["total_portfolio_value"],
                                                 "trades": plan["trades"], "notes": plan["notes"]})
        return plan


def _autopilot_scheduler():
    """Runs every enabled autopilot once per trading day, from 10:00 New York
    time until 30 minutes before the close (13:00 on early-close days), never
    on weekends or NYSE holidays. Prices are the same ~15-minute-delayed
    quotes as the rest of the app."""
    while True:
        time.sleep(AUTOPILOT_CHECK_SECONDS)
        now = datetime.now(MARKET_TZ)
        today = now.date().isoformat()
        market = market_status(now)  # weekends, NYSE holidays and early closes
        hours = market_session(now.date())
        if hours and now >= hours[1] + CLOSE_SNAPSHOT_DELAY:
            _record_closing_values(today)
        if not market["open"] or now.time() < AUTOPILOT_START or \
                now > datetime.fromisoformat(market["closes_at"]) - AUTOPILOT_STOP_BEFORE_CLOSE:
            continue
        try:
            due = engine.autopilot_portfolios()
        except Exception as e:
            print(f"[warn] autopilot: can't list portfolios: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for portfolio_id, user_id in due:
            try:
                if engine.get_autopilot(portfolio_id, user_id).get("last_scheduled_date") == today:
                    continue
                plan = _run_autopilot(portfolio_id, user_id, execute=True, trigger="scheduled")
                if not plan.get("retry"):  # stale prices: try again at the next check
                    engine.update_autopilot(portfolio_id, user_id, {"last_scheduled_date": today})
            except Exception as e:  # one bad portfolio mustn't stop the others
                print(f"[warn] autopilot run failed for portfolio {portfolio_id}: {type(e).__name__}: {e}", file=sys.stderr)


def _record_closing_values(today):
    """Saves each autopilot portfolio's value after the close, once a day. The
    daily loss limit compares against the last value saved before today; without
    this, that was whenever someone last opened the Portfolio page."""
    if market_data.status()["stale"]:
        return
    try:
        due = engine.autopilot_portfolios()
    except Exception as e:
        print(f"[warn] closing values: can't list portfolios: {type(e).__name__}: {e}", file=sys.stderr)
        return
    for portfolio_id, user_id in due:
        try:
            if engine.get_autopilot(portfolio_id, user_id).get("last_close_recorded") == today:
                continue
            summary = engine.get_portfolio_summary(portfolio_id, user_id, price_for=_live_price)
            engine.record_daily_snapshot(portfolio_id, summary, as_of=today)
            engine.update_autopilot(portfolio_id, user_id, {"last_close_recorded": today})
        except Exception as e:
            print(f"[warn] closing value failed for portfolio {portfolio_id}: {type(e).__name__}: {e}", file=sys.stderr)


@app.get("/portfolio/{portfolio_id}/autopilot")
def get_autopilot(portfolio_id: int, user_id: int = Depends(get_current_user)):
    """Settings, status and the last 20 runs of this portfolio's autopilot."""
    return _portfolio_errors(lambda: _autopilot_view(portfolio_id, user_id))


@app.put("/portfolio/{portfolio_id}/autopilot")
def update_autopilot(portfolio_id: int, req: AutopilotUpdate, user_id: int = Depends(get_current_user)):
    """Turns the autopilot on or off and changes its limits. Turning it on
    clears a pause from the daily loss limit."""
    def apply():
        state = engine.get_autopilot(portfolio_id, user_id)
        changes = req.model_dump(exclude_none=True)
        settings = {**state.get("settings", {}), **{k: v for k, v in changes.items() if k != "enabled"}}
        update = {"settings": clamp_settings(settings)}
        if "enabled" in changes:
            update["enabled"] = changes["enabled"]
            if changes["enabled"]:
                update["paused_reason"] = None
        engine.update_autopilot(portfolio_id, user_id, update)
        return _autopilot_view(portfolio_id, user_id)
    return _portfolio_errors(apply)


@app.post("/portfolio/{portfolio_id}/autopilot/preview")
def preview_autopilot(portfolio_id: int, user_id: int = Depends(get_current_user)):
    """The trades the autopilot would make right now, with reasons. Places nothing."""
    return _portfolio_errors(lambda: _run_autopilot(portfolio_id, user_id, execute=False, trigger="preview"))


@app.post("/portfolio/{portfolio_id}/autopilot/run")
def run_autopilot(portfolio_id: int, user_id: int = Depends(get_current_user)):
    """Runs the autopilot once now (even while it's switched off) and logs the
    run. Refused while the market is closed."""
    return _portfolio_errors(lambda: _run_autopilot(portfolio_id, user_id, execute=True, trigger="manual"))


@app.get("/portfolio/{portfolio_id}/history")
def get_history(portfolio_id: int, user_id: int = Depends(get_current_user)):
    try:
        return {"history": engine.get_history(portfolio_id, user_id)}
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/portfolio/{portfolio_id}/trade-history")
def get_trade_history(portfolio_id: int, user_id: int = Depends(get_current_user)):
    try:
        return {"trades": engine.get_trade_history(portfolio_id, user_id)}
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/")
def root():
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    return {"status": "ok"}


@app.get("/market.html")
def market_page():
    page = frontend_dir / "market.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Market page not found")


@app.get("/portfolio.html")
def portfolio_page():
    page = frontend_dir / "portfolio.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Portfolio page not found")


@app.get("/sentiment.html")
def sentiment_page():
    page = frontend_dir / "sentiment.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Sentiment page not found")


@app.get("/options.html")
def options_page():
    page = frontend_dir / "options.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Options page not found")


@app.get("/billing.html")
def billing_page():
    page = frontend_dir / "billing.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Billing page not found")


@app.get("/login.html")
def login_page():
    page = frontend_dir / "login.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Login page not found")


@app.get("/autopilot.html")
def autopilot_page():
    page = frontend_dir / "autopilot.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Autopilot page not found")


@app.get("/profile.html")
def profile_page():
    page = frontend_dir / "profile.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Profile page not found")


@app.get("/styles.css")
def styles_css():
    css_path = frontend_dir / "styles.css"
    if css_path.exists():
        return FileResponse(css_path, media_type="text/css", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Stylesheet not found")


@app.get("/api.js")
def api_js():
    js_path = frontend_dir / "api.js"
    if js_path.exists():
        return FileResponse(js_path, media_type="text/javascript", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Script not found")


@app.get("/ticker-picker.js")
def ticker_picker_js():
    js_path = frontend_dir / "ticker-picker.js"
    if js_path.exists():
        return FileResponse(js_path, media_type="text/javascript", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Script not found")


@app.get("/favicon.ico")
@app.get("/favicon.svg")
def favicon():
    return FileResponse(frontend_dir / "favicon.svg", media_type="image/svg+xml")


@app.get("/health")
def health():
    return {"status": "ok"}


# Every open sentiment tab polls this endpoint, so without a cache each poll
# re-fetched all RSS feeds, re-ran Claude, and rewrote the history file.
# The lock also means only one request at a time writes sentiment_history.json.
SENTIMENT_TTL_SECONDS = 300
SENTIMENT_MIN_REFRESH_SECONDS = 60  # a manual refresh can't refetch more often than this
_sentiment_cache = {"data": None, "at": 0.0}
_sentiment_lock = threading.Lock()


def _sentiment_data(rows, refresh=False):
    """(cached headline data, its age in seconds), refetched when stale."""
    with _sentiment_lock:
        age = time.monotonic() - _sentiment_cache["at"]
        stale = age > (SENTIMENT_MIN_REFRESH_SECONDS if refresh else SENTIMENT_TTL_SECONDS)
        if _sentiment_cache["data"] is None or stale:
            _sentiment_cache["data"] = collect_sentiment_results(
                extra_companies=company_aliases(rows),
                known_tickers={row["ticker"] for row in rows} or None,
            )
            _sentiment_cache["at"] = time.monotonic()
            age = 0.0
        return _sentiment_cache["data"], age


@app.get("/news-sentiment")
def get_news_sentiment(refresh: bool = False):
    """Returns current news sentiment for tracked tickers and sector themes,
    plus `suggestions`: the stocks and sectors in the news next to their
    present market condition. If the live RSS feed yields no relevant
    headlines, this returns a curated demo fallback so the frontend still
    shows meaningful sentiment.

    Headlines are cached for SENTIMENT_TTL_SECONDS; refresh=true refetches
    sooner, but not more than once a minute. Suggestions are rebuilt on every
    request so prices are always the latest snapshot."""
    rows = market_data.snapshot()
    data, age = _sentiment_data(rows, refresh)
    # Built per request (not stored in the cache) so prices are always current.
    by_sector = largest_by_sector(rows)
    results = [{**a, "affected": affected_stocks(a, market_data.lookup, by_sector)} for a in data["results"]]
    return {
        **data,
        "results": results,
        "suggestions": build_suggestions(data["results"], market_data.lookup, rows),
        "next_refresh_in": max(0, round(SENTIMENT_TTL_SECONDS - age)),
    }


# Daily closes change once a day, so one download per stock per hour is plenty.
PRICE_HISTORY_TTL_SECONDS = 60 * 60
PRICE_HISTORY_DAYS = 90  # same window (and start close) as momentum_pct
_price_history_cache = {}  # ticker -> (fetched_at, points)


@app.get("/price-history/{ticker}")
def get_price_history(ticker: Annotated[str, FastAPIPath(pattern=TICKER_PATTERN)]):
    """Daily closes for the momentum window, oldest first, for the Market
    page chart. Cached per ticker for PRICE_HISTORY_TTL_SECONDS."""
    ticker = _normalize_ticker(ticker)
    cached = _price_history_cache.get(ticker)
    if cached and time.monotonic() - cached[0] < PRICE_HISTORY_TTL_SECONDS:
        return {"ticker": ticker, "points": cached[1]}
    try:
        closes = fetch_price_history([ticker], lookback_days=PRICE_HISTORY_DAYS)
    except Exception as e:  # network error, Yahoo rate limit
        raise HTTPException(status_code=502, detail=f"Could not fetch price history: {e}")
    series = closes[ticker].dropna().tail(PRICE_HISTORY_DAYS) if ticker in closes else None
    points = [] if series is None else [
        {"date": day.date().isoformat(), "close": round(float(close), 2)} for day, close in series.items()]
    if not points:
        raise HTTPException(status_code=404, detail=f"No price history for {ticker}")
    _price_history_cache[ticker] = (time.monotonic(), points)
    return {"ticker": ticker, "points": points}


@app.get("/fundamentals/{ticker}")
def get_company_fundamentals(ticker: Annotated[str, FastAPIPath(pattern=TICKER_PATTERN)]):
    """Revenue, earnings, cash flow and debt from SEC filings, plus links to
    the latest 10-K/10-Q/8-K (or 20-F/6-K for foreign filers). Cached per
    company for 6 hours; see fundamentals/sec_edgar.py."""
    ticker = _normalize_ticker(ticker)
    try:
        data = get_fundamentals(ticker)
    except Exception as e:  # network error, SEC rate limit or outage
        raise HTTPException(status_code=502, detail=f"Could not reach SEC EDGAR: {e}")
    if data is None:
        raise HTTPException(status_code=404, detail=f"No SEC filings found for {ticker}")
    return data


# Each explanation can cost a Claude call, so one per stock per 15 minutes.
EXPLAIN_TTL_SECONDS = 15 * 60
EXPLAIN_CACHE_MAX = 500
_explain_cache = {}  # ticker -> (made_at, response)


@app.get("/explain/{ticker}")
def explain_stock(ticker: Annotated[str, FastAPIPath(pattern=TICKER_PATTERN)],
                  user_id: int = Depends(get_current_user)):
    """A plain-English explanation of one stock: what is going on, why it is
    moving, its trend, its financials and the risks to watch, from the
    app's own prices, headlines and SEC filings (see explain/stock_explainer.py).
    Written by Claude when ANTHROPIC_API_KEY is set, otherwise by fixed
    rules. Requires sign-in, since it can spend API credit."""
    ticker = _normalize_ticker(ticker)
    cached = _explain_cache.get(ticker)
    if cached and time.monotonic() - cached[0] < EXPLAIN_TTL_SECONDS:
        return cached[1]
    row = market_data.lookup(ticker)
    if not row:
        raise HTTPException(status_code=404, detail=f"{ticker} is not in the tracked market list")
    rows = market_data.snapshot()
    try:
        news, _ = _sentiment_data(rows)
    except Exception as e:  # feeds down: explain from prices and filings alone
        print(f"[warn] news unavailable for /explain: {type(e).__name__}", file=sys.stderr)
        news = {}
    headlines = [] if news.get("fallback") else news.get("results", [])  # demo headlines aren't real news
    try:
        fundamentals = get_fundamentals(ticker)
    except Exception:  # SEC rate limit or outage
        fundamentals = None
    facts = build_facts(
        row, rows, headlines, fundamentals,
        keyword_tone=any(str(a.get("reasoning", "")).startswith("Heuristic") for a in headlines),
    )

    explanation, source = None, "rules"
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key and Anthropic is not None:
        try:
            explanation = explain_with_claude(Anthropic(api_key=api_key), facts)
            source = "claude" if explanation else source
        except Exception as e:  # rate limit, network, bad key
            print(f"[warn] Claude explanation failed, using rules: {type(e).__name__}: {e}", file=sys.stderr)
    if explanation is None:
        explanation = explain_with_rules(facts)

    response = {
        "ticker": ticker,
        "explanation": explanation.model_dump(),
        "source": source,
        "facts": facts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if len(_explain_cache) >= EXPLAIN_CACHE_MAX:
        _explain_cache.clear()
    _explain_cache[ticker] = (time.monotonic(), response)
    return response


# ---------------------------------------------------------------------------
# Politician trading endpoints
# ---------------------------------------------------------------------------

@app.get("/politicians")
def get_politicians():
    """List every politician with at least one disclosed trade."""
    return {"politicians": list_politicians()}


@app.get("/politicians/{name}/trades")
def get_politician_trades(name: str):
    """All trades for one politician, newest transaction date first."""
    trades = get_trades_for_politician(name)
    if not trades:
        raise HTTPException(status_code=404, detail=f"No trades found for '{name}'")
    return {"politician": name, "count": len(trades), "trades": trades}


@app.post("/politicians/refresh")
def refresh_politician_data(user_id: int = Depends(get_current_user)):
    """Pulls fresh data from Quiver Quantitative into the local cache.
    Requires sign-in so anonymous callers can't burn through the Quiver quota."""
    try:
        count = refresh_cache()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not refresh from Quiver API: {e}")
    return {"refreshed": count}
