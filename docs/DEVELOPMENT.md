# Developing locally

A short checklist. The [README](../README.md) has the full API and project layout.

## You need

- **Python 3.11+** (tested on 3.14). On macOS use `python3`, since there's no `python`.
- **Git**
- An internet connection. Market data comes from Nasdaq and yfinance, and news from RSS feeds.
- **Docker**, for a local PostgreSQL.

Accounts, portfolios and trades live in PostgreSQL (Cloud SQL in production, on a private IP only your VM can reach). Locally you run your own Postgres in a container, so you never touch real data. Files (market cache, sentiment history, profile photos) go to a local `.data/` folder instead of the storage bucket, and the frontend is plain HTML/JS served by the API.

## Setup (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # app + test tools
```

## Run

Start PostgreSQL once (it keeps running in the background; local connections are trusted, so there's no password):

```bash
docker run -d --name mahokshahvata-pg -p 127.0.0.1:5433:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=app -e POSTGRES_DB=mahokshahvata postgres:16-alpine
```

Then the app (it creates the tables on first start, from `db/migrations/`):

```bash
DB_PORT=5433 USE_DEMO_AUTH=true .venv/bin/uvicorn api.main:app --reload
```

Or run everything in containers with `docker compose up --build` (see the README). The production database is on a private IP, so it can't be reached from a laptop at all.

- Dashboard: http://localhost:8000/ (sign in or create an account first; "Continue with demo account" needs `USE_DEMO_AUTH=true`), API docs: http://localhost:8000/docs
- The first start takes ~2 minutes before stock data appears (`/momentum` reports `warming_up: true`).
  For faster restarts while developing, add `MARKET_UNIVERSE_SIZE=100`.
- Run a single process only (no `--workers`): the market-data thread and the autopilot scheduler run inside the app.
- Schema changes: add `db/migrations/002_<what>.sql`; never edit an applied migration. `python -m db.migrate --status` lists them.

## Keys (all optional)

Put them in a `.env` file in the project folder (loaded on startup; shell variables take priority) or export them in your shell. `.env` is gitignored; never commit keys.

| Variable | Without it |
|---|---|
| `ANTHROPIC_API_KEY` | News sentiment falls back to keyword matching, which is often wrong |
| `QUIVER_API_KEY` | Politician trades stay empty |
| `MASSIVE_API_KEY` | Prices come from the free Nasdaq screener and yfinance (~15 min delayed) |
| `BENZINGA_API_KEY` | News comes from RSS feeds, matched to companies by name |
| `SEC_USER_AGENT` | Fundamentals still work but send a placeholder contact. SEC asks for your app name + email |
| `FIREBASE_PROJECT_ID` | Defaults to `mahokshahvata-ae5f9`; "Continue with Google" checks tokens for this project (no key file needed) |

On the VM there is no `.env`: `SECRETS_PROJECT` makes the app load the same keys (and the database password) from Secret Manager at startup (`cloud/secrets.py`).

## Test

```bash
.venv/bin/python -m pytest -q
```

Start the local PostgreSQL first (see Run); tests use `DB_PORT=5433` by default and fail fast with a reminder if it isn't running. Each test engine gets its own schema with the migrations applied (dropped afterwards), and other tests use temp folders and canned API responses, so nothing touches real data or needs keys. CI runs the same suite against a Postgres service container.

## Local data

Database rows are in your local Postgres (`docker rm -f mahokshahvata-pg` resets it). Files are in `.data/` (gitignored; on the VM they're in the storage bucket). Delete one to reset it.

| File | Holds |
|---|---|
| `.data/market_cache.json` | Market snapshot, so restarts are instant |
| `.data/sentiment_history.json` | Recent sentiment results |
| `.data/avatars/<user>/` | Profile photos |

## Where to change things

| To change | Edit |
|---|---|
| Endpoints | `api/main.py` |
| Trading rules, P/L | `paper_trading/paper_trading_engine.py` |
| Database tables | a new `db/migrations/00N_*.sql` |
| Stock universe, prices (Nasdaq / yfinance / Massive) | `momentum/market_data.py` |
| Fundamentals and SEC filings | `fundamentals/sec_edgar.py` |
| Momentum math, sector/theme filters | `momentum/momentum_engine.py` |
| Sign-in, sessions, account menu (all pages) | `frontend/api.js`, `frontend/login.html`, `frontend/profile.html` |
| Accounts and sessions on the server | `/auth/*` in `api/main.py`, `auth/passwords.py`, users/sessions methods in `paper_trading_engine.py` |
| Styling | `frontend/styles.css` |

Open issues and the roadmap are in [PROJECT_STATUS.md](PROJECT_STATUS.md).
