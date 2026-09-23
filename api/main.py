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
    GET  /portfolio                            list the signed-in user's portfolios
    POST /portfolio                            create a portfolio
    POST /portfolio/{id}/build                 build it from a momentum list
    GET  /portfolio/{id}                       get portfolio summary
    POST /portfolio/{id}/mark-to-market         update with current prices
"""

import os
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Make sibling modules importable
sys.path.append(str(Path(__file__).parent.parent / "momentum"))
sys.path.append(str(Path(__file__).parent.parent / "paper_trading"))
sys.path.append(str(Path(__file__).parent.parent / "politician_trades"))
sys.path.append(str(Path(__file__).parent.parent / "auth"))
sys.path.append(str(Path(__file__).parent.parent / "news_sentiment"))

from momentum_engine import rank_momentum, latest_price, DEFAULT_UNIVERSE, DEFAULT_SECTOR_LOOKUP
from paper_trading_engine import PaperTradingEngine, RiskRules
from politician_trades import list_politicians, get_trades_for_politician, refresh_cache
from firebase_auth import verify_firebase_token
from news_sentiment_scraper import collect_sentiment_results

app = FastAPI(title="Mahokshahvata API", version="0.1.0")
frontend_dir = Path(__file__).parent.parent / "frontend"
engine = PaperTradingEngine(str(Path(__file__).parent.parent / "paper_trading" / "paper_trading.db"))

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


# ---------------------------------------------------------------------------
# Auth dependency — every portfolio endpoint requires a valid session token
# ---------------------------------------------------------------------------

def get_current_user(authorization: str = Header(None)):
    """Reads 'Authorization: Bearer <Firebase ID token>', verifies it with
    Firebase, and returns our internal user_id — creating the user record
    on their very first authenticated call if this is a new sign-in.
    Raises 401 if missing or invalid. FastAPI's Depends() runs this before
    the endpoint body, so an endpoint that declares this dependency can
    assume the caller is already authenticated by the time its own code
    runs.

    In local demo mode, we silently fall back to the default demo token so the
    browser can still exercise the app without a real Firebase login configured."""
    demo_mode = os.getenv("USE_DEMO_AUTH", "").lower() in {"1", "true", "yes", "on"}

    if not authorization or not authorization.startswith("Bearer "):
        if demo_mode:
            token = "demo-token"
        else:
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    else:
        token = authorization.removeprefix("Bearer ").strip()

    try:
        firebase_user = verify_firebase_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return engine.get_or_create_user(
        firebase_uid=firebase_user["firebase_uid"],
        email=firebase_user["email"],
        name=firebase_user["name"],
    )


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class CreatePortfolioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    starting_capital: float = Field(gt=0, description="Must be a positive amount")


class BuildPortfolioRequest(BaseModel):
    max_positions: int = Field(default=5, gt=0, le=20)
    max_allocation_per_stock: float = Field(default=0.30, gt=0, le=1.0)
    max_allocation_per_sector: float = Field(default=0.50, gt=0, le=1.0)


class MarkToMarketRequest(BaseModel):
    prices: dict  # {"NVDA": 190.12, "PLTR": 42.5, ...}
    option_prices: dict | None = None


class AddCashRequest(BaseModel):
    amount: float = Field(gt=0, description="Must be a positive amount")


class TradeStockRequest(BaseModel):
    ticker: str
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    side: str = Field(default="BUY", pattern="^(BUY|SELL)$")


class TradeOptionRequest(BaseModel):
    ticker: str
    option_type: str = Field(..., pattern="^(CALL|PUT)$")
    strike: float = Field(gt=0)
    expiry: str
    quantity: int = Field(gt=0)
    premium: float = Field(gt=0)
    side: str = Field(default="BUY", pattern="^(BUY|SELL)$")


# ---------------------------------------------------------------------------
# Momentum endpoints
# ---------------------------------------------------------------------------

@app.get("/momentum")
def get_momentum(top: int = 10, lookback_days: int = 90, sector: str | None = None, q: str | None = None):
    """Returns the top-N ranked momentum stocks from the default universe.
    No sign-in required — momentum data isn't tied to any one user.

    Supports optional client-side filters by sector and ticker search term."""
    ranked = rank_momentum(DEFAULT_UNIVERSE, sector_lookup=DEFAULT_SECTOR_LOOKUP, lookback_days=lookback_days)
    if sector:
        ranked = [item for item in ranked if item.get("sector", "Unknown").lower() == sector.lower()]
    if q:
        needle = q.strip().lower()
        ranked = [item for item in ranked if needle in item.get("ticker", "").lower() or needle in item.get("sector", "").lower()]
    limited = ranked[:top]
    return {"count": len(limited), "results": limited}


# ---------------------------------------------------------------------------
# Portfolio (paper trading) endpoints — every one requires sign-in, and every
# one is scoped to the signed-in user's own portfolios only.
# ---------------------------------------------------------------------------

@app.get("/portfolio")
def list_my_portfolios(user_id: int = Depends(get_current_user)):
    return {"portfolios": engine.list_portfolios_for_user(user_id)}


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

    momentum_list = rank_momentum(DEFAULT_UNIVERSE, lookback_days=90)
    if not momentum_list:
        raise HTTPException(status_code=502, detail="Could not fetch momentum data")

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


@app.get("/portfolio/{portfolio_id}")
def get_portfolio(portfolio_id: int, user_id: int = Depends(get_current_user)):
    """Returns the portfolio's current state. A freshly created portfolio
    with no holdings yet is a normal, valid response — not a 404. Only a
    portfolio that truly doesn't exist (or belongs to someone else) errors."""
    try:
        return engine.get_portfolio_summary(portfolio_id, user_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError:
        raise HTTPException(status_code=404, detail="Portfolio not found")


@app.post("/portfolio/{portfolio_id}/mark-to-market")
def mark_to_market(portfolio_id: int, req: MarkToMarketRequest, user_id: int = Depends(get_current_user)):
    try:
        return engine.mark_to_market(portfolio_id, user_id, req.prices, current_option_prices=req.option_prices)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/portfolio/{portfolio_id}/cash")
def add_cash_to_portfolio(portfolio_id: int, req: AddCashRequest, user_id: int = Depends(get_current_user)):
    try:
        return engine.add_cash_balance(portfolio_id, user_id, req.amount)
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# How far a submitted trade price may drift from the last market close.
# Covers intraday moves; stops a client from buying at $0.01.
MAX_PRICE_DEVIATION = 0.05


@app.post("/portfolio/{portfolio_id}/trade-stock")
def trade_stock(portfolio_id: int, req: TradeStockRequest, user_id: int = Depends(get_current_user)):
    ticker = req.ticker.strip().upper()
    market_price = latest_price(ticker)
    if market_price is None:
        raise HTTPException(status_code=502, detail=f"Could not fetch a market price for {ticker}")
    if abs(req.price - market_price) / market_price > MAX_PRICE_DEVIATION:
        raise HTTPException(
            status_code=400,
            detail=f"Price ${req.price:.2f} is more than {MAX_PRICE_DEVIATION:.0%} away from the market price ${market_price:.2f}",
        )
    try:
        return engine.trade_stock(
            portfolio_id,
            user_id,
            ticker,
            req.quantity,
            req.price,
            req.side,
            sector=DEFAULT_SECTOR_LOOKUP.get(ticker, "Unknown"),
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/portfolio/{portfolio_id}/trade-option")
def trade_option(portfolio_id: int, req: TradeOptionRequest, user_id: int = Depends(get_current_user)):
    try:
        return engine.trade_option(
            portfolio_id,
            user_id,
            req.ticker,
            req.option_type,
            req.strike,
            req.expiry,
            req.quantity,
            req.premium,
            req.side,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="This portfolio doesn't belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
        return FileResponse(index_path)
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


@app.get("/styles.css")
def styles_css():
    css_path = frontend_dir / "styles.css"
    if css_path.exists():
        return FileResponse(css_path, media_type="text/css", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    raise HTTPException(status_code=404, detail="Stylesheet not found")


@app.get("/health")
def health():
    return {"status": "ok"}


# Every open sentiment tab polls this endpoint, so without a cache each poll
# re-fetched all RSS feeds, re-ran Claude, and rewrote the history file.
# The lock also means only one request at a time writes sentiment_history.json.
SENTIMENT_TTL_SECONDS = 300
_sentiment_cache = {"data": None, "at": 0.0}
_sentiment_lock = threading.Lock()


@app.get("/news-sentiment")
def get_news_sentiment():
    """Returns current news sentiment for tracked tickers and sector themes.
    If the live RSS feed yields no relevant headlines, this returns a curated
    demo fallback so the frontend still shows meaningful sentiment.
    Results are cached for SENTIMENT_TTL_SECONDS."""
    with _sentiment_lock:
        if _sentiment_cache["data"] is None or time.monotonic() - _sentiment_cache["at"] > SENTIMENT_TTL_SECONDS:
            _sentiment_cache["data"] = collect_sentiment_results()
            _sentiment_cache["at"] = time.monotonic()
        return _sentiment_cache["data"]


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
