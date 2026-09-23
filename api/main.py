"""
api/main.py

Backend API layer tying together the momentum engine, news sentiment
scraper, and paper trading engine — the "Backend API Layer" from the
architecture doc (step 9), the piece that connects data, analytics, AI
and portfolio services to a frontend.

Setup:
    pip install fastapi uvicorn yfinance pandas anthropic feedparser

Run:
    export ANTHROPIC_API_KEY="your-key-here"
    uvicorn api.main:app --reload

Then visit http://localhost:8000/docs for interactive API docs (FastAPI
generates this automatically).

Endpoints:
    GET  /momentum?top=10                     ranked momentum stocks
    POST /portfolio                            create a portfolio
    POST /portfolio/{id}/build                 build it from a momentum list
    GET  /portfolio/{id}                       get portfolio summary
    POST /portfolio/{id}/mark-to-market         update with current prices
"""

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Make sibling modules importable
sys.path.append(str(Path(__file__).parent.parent / "momentum"))
sys.path.append(str(Path(__file__).parent.parent / "paper_trading"))
sys.path.append(str(Path(__file__).parent.parent / "politician_trades"))
sys.path.append(str(Path(__file__).parent.parent / "auth"))

from momentum_engine import rank_momentum, DEFAULT_UNIVERSE
from paper_trading_engine import PaperTradingEngine, RiskRules
from politician_trades import list_politicians, get_trades_for_politician, refresh_cache
from google_auth import verify_google_id_token, issue_session_token, verify_session_token

app = FastAPI(title="AI Stock Platform API", version="0.1.0")
engine = PaperTradingEngine(str(Path(__file__).parent.parent / "paper_trading" / "paper_trading.db"))

# CORS: without this, a browser blocks every call from your frontend to this
# API once they're on different origins (different port counts as different
# origin too — localhost:5500 calling localhost:8000 is already cross-origin).
# ALLOWED_ORIGINS is a comma-separated env var, e.g.
#   export ALLOWED_ORIGINS="https://your-dashboard.com,http://localhost:5500"
# Defaults to "*" (allow anything) for easy local development — TIGHTEN THIS
# to your real frontend's exact domain(s) before deploying publicly, or
# anyone can call your API from any website.
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allowed_origins == "*" else _allowed_origins.split(","),
    allow_credentials=False,  # we use a Bearer token, not cookies, so this can stay False
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth dependency — every portfolio endpoint requires a valid session token
# ---------------------------------------------------------------------------

def get_current_user(authorization: str = Header(None)):
    """Reads 'Authorization: Bearer <session token>', verifies it, and
    returns the internal user_id. Raises 401 if missing or invalid —
    FastAPI's Depends() runs this before the endpoint body, so an
    endpoint that declares this dependency can assume the caller is
    already authenticated by the time its own code runs."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return verify_session_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class GoogleSignInRequest(BaseModel):
    id_token: str  # the ID token Google Identity Services gave the frontend


class CreatePortfolioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    starting_capital: float = Field(gt=0, description="Must be a positive amount")


class BuildPortfolioRequest(BaseModel):
    max_positions: int = Field(default=5, gt=0, le=20)
    max_allocation_per_stock: float = Field(default=0.30, gt=0, le=1.0)
    max_allocation_per_sector: float = Field(default=0.50, gt=0, le=1.0)


class MarkToMarketRequest(BaseModel):
    prices: dict  # {"NVDA": 190.12, "PLTR": 42.5, ...}


# ---------------------------------------------------------------------------
# Auth endpoint
# ---------------------------------------------------------------------------

@app.post("/auth/google")
def sign_in_with_google(req: GoogleSignInRequest):
    """Frontend sends the ID token Google gave it after the user signed in.
    We verify it really came from Google and for this app, then issue our
    own session token for the frontend to use on every subsequent call."""
    try:
        google_user = verify_google_id_token(req.id_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    user_id = engine.get_or_create_user(
        google_sub=google_user["google_sub"],
        email=google_user["email"],
        name=google_user["name"],
    )
    session_token = issue_session_token(user_id)
    return {
        "session_token": session_token,
        "user": {"name": google_user["name"], "email": google_user["email"], "picture": google_user["picture"]},
    }


# ---------------------------------------------------------------------------
# Momentum endpoints
# ---------------------------------------------------------------------------

@app.get("/momentum")
def get_momentum(top: int = 10, lookback_days: int = 90):
    """Returns the top-N ranked momentum stocks from the default universe.
    No sign-in required — momentum data isn't tied to any one user."""
    ranked = rank_momentum(DEFAULT_UNIVERSE, lookback_days=lookback_days)
    return {"count": len(ranked[:top]), "results": ranked[:top]}


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
        return engine.mark_to_market(portfolio_id, user_id, req.prices)
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


@app.get("/health")
def health():
    return {"status": "ok"}


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
def refresh_politician_data():
    """Pulls fresh data from Quiver Quantitative into the local cache."""
    try:
        count = refresh_cache()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not refresh from Quiver API: {e}")
    return {"refreshed": count}
