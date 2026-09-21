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

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Make sibling modules importable
sys.path.append(str(Path(__file__).parent.parent / "momentum"))
sys.path.append(str(Path(__file__).parent.parent / "paper_trading"))

from momentum_engine import rank_momentum, DEFAULT_UNIVERSE
from paper_trading_engine import PaperTradingEngine, RiskRules

app = FastAPI(title="AI Stock Platform API", version="0.1.0")
engine = PaperTradingEngine(str(Path(__file__).parent.parent / "paper_trading" / "paper_trading.db"))


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class CreatePortfolioRequest(BaseModel):
    name: str
    starting_capital: float


class BuildPortfolioRequest(BaseModel):
    max_positions: int = 5
    max_allocation_per_stock: float = 0.30
    max_allocation_per_sector: float = 0.50


class MarkToMarketRequest(BaseModel):
    prices: dict  # {"NVDA": 190.12, "PLTR": 42.5, ...}


# ---------------------------------------------------------------------------
# Momentum endpoints
# ---------------------------------------------------------------------------

@app.get("/momentum")
def get_momentum(top: int = 10, lookback_days: int = 90):
    """Returns the top-N ranked momentum stocks from the default universe."""
    ranked = rank_momentum(DEFAULT_UNIVERSE, lookback_days=lookback_days)
    return {"count": len(ranked[:top]), "results": ranked[:top]}


# ---------------------------------------------------------------------------
# Portfolio (paper trading) endpoints
# ---------------------------------------------------------------------------

@app.post("/portfolio")
def create_portfolio(req: CreatePortfolioRequest):
    portfolio_id = engine.create_portfolio(req.name, req.starting_capital)
    return {"portfolio_id": portfolio_id}


@app.post("/portfolio/{portfolio_id}/build")
def build_portfolio(portfolio_id: int, req: BuildPortfolioRequest):
    """Builds the portfolio from the current momentum ranking."""
    momentum_list = rank_momentum(DEFAULT_UNIVERSE, lookback_days=90)
    if not momentum_list:
        raise HTTPException(status_code=502, detail="Could not fetch momentum data")

    rules = RiskRules(
        max_positions=req.max_positions,
        max_allocation_per_stock=req.max_allocation_per_stock,
        max_allocation_per_sector=req.max_allocation_per_sector,
    )
    try:
        purchases = engine.build_portfolio_from_momentum(portfolio_id, momentum_list, rules)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"portfolio_id": portfolio_id, "purchases": purchases}


@app.get("/portfolio/{portfolio_id}")
def get_portfolio(portfolio_id: int):
    summary = engine.get_portfolio_summary(portfolio_id)
    if summary["latest"] is None and not summary["holdings"]:
        raise HTTPException(status_code=404, detail="Portfolio not found or empty")
    return summary


@app.post("/portfolio/{portfolio_id}/mark-to-market")
def mark_to_market(portfolio_id: int, req: MarkToMarketRequest):
    snapshot = engine.mark_to_market(portfolio_id, req.prices)
    return snapshot


@app.get("/portfolio/{portfolio_id}/history")
def get_history(portfolio_id: int):
    return {"history": engine.get_history(portfolio_id)}


@app.get("/health")
def health():
    return {"status": "ok"}
