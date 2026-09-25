# Developing locally

A short checklist. The [README](../README.md) has the full API and project layout.

## You need

- **Python 3.11+** (tested on 3.14). On macOS use `python3`, since there's no `python`.
- **Git**
- An internet connection. Market data comes from Nasdaq and yfinance, and news from RSS feeds.
- Optional: the `sqlite3` CLI for inspecting the database (preinstalled on macOS).

No database server, Docker or Node is needed. Storage is SQLite and JSON files, and the frontend is plain HTML/JS served by the API.

## Setup (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
USE_DEMO_AUTH=true .venv/bin/uvicorn api.main:app --reload
```

- Dashboard: http://localhost:8000/ (sign in or create an account first; "Continue with demo account" needs `USE_DEMO_AUTH=true`), API docs: http://localhost:8000/docs
- The first start takes ~2 minutes before stock data appears (`/momentum` reports `warming_up: true`).
  For faster restarts while developing, add `MARKET_UNIVERSE_SIZE=100`.
- Run a single process only (no `--workers`). The trade lock is per-process.

## Keys (all optional)

Put them in a `.env` file in the project folder (loaded on startup; shell variables take priority) or export them in your shell. `.env` is gitignored; never commit keys.

| Variable | Without it |
|---|---|
| `ANTHROPIC_API_KEY` | News sentiment falls back to keyword matching, which is often wrong |
| `QUIVER_API_KEY` | Politician trades stay empty |
| `MASSIVE_API_KEY` | Prices come from the free Nasdaq screener and yfinance (~15 min delayed) |
| `BENZINGA_API_KEY` | News comes from RSS feeds, matched to companies by name |
| `SEC_USER_AGENT` | Fundamentals still work but send a placeholder contact. SEC asks for your app name + email |
| `GOOGLE_APPLICATION_CREDENTIALS` | Google (Firebase) tokens aren't accepted. Email/password and demo sign-in still work |

## Test

```bash
.venv/bin/python -m pytest -q
```

Tests use temp files and canned API responses, so they don't touch your local data or need keys.

## Local data

All of these are created at runtime and gitignored. Delete one to reset it.

| File | Holds |
|---|---|
| `paper_trading/paper_trading.db` | Users (with hashed passwords), sessions, portfolios, holdings, trades, daily snapshots |
| `politician_trades/politician_trades.db` | Cached congressional trade disclosures |
| `momentum/market_cache.json` | Market snapshot, so restarts are instant |
| `news_sentiment/sentiment_history.json` | Recent sentiment results |

## Where to change things

| To change | Edit |
|---|---|
| Endpoints | `api/main.py` |
| Trading rules, P/L, schema | `paper_trading/paper_trading_engine.py` |
| Stock universe, prices (Nasdaq / yfinance / Massive) | `momentum/market_data.py` |
| Fundamentals and SEC filings | `fundamentals/sec_edgar.py` |
| Momentum math, sector/theme filters | `momentum/momentum_engine.py` |
| Sign-in, sessions, account menu (all pages) | `frontend/api.js`, `frontend/login.html`, `frontend/profile.html` |
| Accounts and sessions on the server | `/auth/*` in `api/main.py`, `auth/passwords.py`, users/sessions methods in `paper_trading_engine.py` |
| Styling | `frontend/styles.css` |

Open issues and the roadmap are in [PROJECT_STATUS.md](PROJECT_STATUS.md).
