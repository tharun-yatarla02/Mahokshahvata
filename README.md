# AI-Powered Stock Analysis & Momentum Investment Platform

An end-to-end demo combining a momentum ranking engine, LLM-based news
sentiment analysis, politician trade tracking, and a paper (simulated)
trading engine for stocks and options, behind a FastAPI backend that also
serves a multi-page web dashboard.

No real money is traded — every portfolio is simulated.

> **New to the project?** Start with [docs/HANDOFF.md](docs/HANDOFF.md):
> setup from a fresh clone, how the code fits together, gotchas, and where
> to pick up. Progress and open work: [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md).

## Project layout

```
ai_stock_platform/
├── api/
│   └── main.py                      # FastAPI app: REST endpoints + serves the frontend
├── auth/
│   ├── passwords.py                 # password hashing + session tokens for email/password accounts
│   ├── firebase_auth.py             # verifies Firebase ID tokens (plus local demo mode)
│   └── README.md                    # Firebase project setup + frontend sign-in snippet
├── frontend/                        # static dashboard pages, served by the API
│   ├── index.html                   # overview
│   ├── market.html                  # searchable stock trading + market details
│   ├── portfolio.html               # holdings, sector allocation, trade history
│   ├── sentiment.html               # live news sentiment feed
│   ├── options.html                 # option trades + history
│   ├── billing.html                 # add cash to a portfolio
│   ├── styles.css                   # shared light/dark theme
│   └── ticker-picker.js             # ticker autocomplete used on Portfolio and Options
├── momentum/
│   ├── momentum_engine.py           # momentum math, sector/theme filters, stock search
│   └── market_data.py               # background service tracking the top ~3000 US stocks
├── fundamentals/
│   └── sec_edgar.py                 # revenue, earnings, cash flow, debt + filing links from SEC EDGAR
├── news_sentiment/
│   ├── news_sentiment_scraper.py    # Benzinga or RSS news -> sentiment via Claude (or local model)
│   ├── company_tickers.csv          # company name -> ticker lookup
│   ├── sentiment_history.json       # recent sentiment history
│   └── model/                       # optional local sentiment model (train + inference)
├── paper_trading/
│   └── paper_trading_engine.py      # simulated portfolios, trades, P/L (SQLite, created on first run)
├── politician_trades/
│   └── politician_trades.py         # congressional trade disclosures via Quiver Quantitative
├── docs/
│   ├── HANDOFF.md                   # developer guide: setup, architecture, gotchas, next tasks
│   └── PROJECT_STATUS.md            # current status and roadmap
├── tests/                           # pytest suite
├── requirements.txt
└── README.md
```

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Environment variables

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | News sentiment | Used to classify headlines with Claude (Haiku 4.5, structured outputs). Without it, a keyword fallback is used |
| `SENTIMENT_BACKEND` | News sentiment | `claude` (default) or `local` — `local` needs a trained model, see `news_sentiment/model/train_sentiment_model.py` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Real sign-in | Path to your Firebase service-account JSON — see [auth/README.md](auth/README.md) |
| `USE_DEMO_AUTH` | Local development | `true` enables the shared demo account ("Continue with demo account" on the sign-in page, token `demo-token`). Leave it off in any shared deployment |
| `QUIVER_API_KEY` | Politician trades | Free-tier key from quiverquant.com, used by `POST /politicians/refresh` |
| `MARKET_UNIVERSE_SIZE` | Market data | How many of the largest US stocks to track (default 3000) |
| `MASSIVE_API_KEY` | Market data (optional) | Use [Massive](https://massive.com) (formerly Polygon.io) for quotes and price history instead of the free Nasdaq/Yahoo sources. See [Data providers](#data-providers) |
| `BENZINGA_API_KEY` | News sentiment (optional) | Use Benzinga's ticker-tagged news instead of RSS feeds |
| `SEC_USER_AGENT` | Fundamentals | SEC requires an app name and contact email, e.g. `"Mahokshahvata you@example.com"`. Defaults to a placeholder; set your own |
| `ALLOWED_ORIGINS` | Hosting the frontend elsewhere | Comma-separated origins allowed by CORS. Defaults to `localhost`/`127.0.0.1` on ports 8000 and 5500 |

Put these in a `.env` file in the project folder; the server loads it on
startup (variables already set in your shell take priority). `.env` is
gitignored, so keys are never committed:

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
USE_DEMO_AUTH=true
```

Never commit keys or the Firebase service-account file; `.gitignore`
already excludes `.env` and `*serviceAccountKey*.json`.

## Running the app

For local development (demo auth enables the "Continue with demo account"
button; email/password accounts work either way):

```bash
# with USE_DEMO_AUTH=true and your ANTHROPIC_API_KEY in .env (see above)
uvicorn api.main:app --reload
```

Then open:

- `http://localhost:8000/` — the dashboard (redirects to sign-in first)
- `http://localhost:8000/docs` — interactive API docs (auto-generated by FastAPI)

### Market data

The server tracks the ~3000 largest US-listed common stocks by market cap
(roughly the Russell 3000) in a background thread started with the app:

- **Stock list and prices** come from Nasdaq's stock screener, refreshed
  every 5 minutes (prices are delayed ~15 minutes).
- **90-day price history** comes from yfinance, refreshed every 6 hours.
  Momentum is the latest price vs. the price 90 trading days ago.

The snapshot is saved to `momentum/market_cache.json` (gitignored), so
restarts are instant. The very first start takes about 2 minutes while the
history downloads; until then `/momentum` returns `status.warming_up: true`.

### Paper-trading rules

Stock trades are market orders, like a real broker's:

- They execute at the current market price (the tracked ~15-min-delayed
  quote); any price the client sends is ignored.
- Order by **shares** (`quantity`, fractions allowed to 6 decimals) or by
  **dollar amount** (`amount`, e.g. "$100 of AAPL").
- Buys need enough cash and sells need enough shares; a rejected order
  says how many shares the cash would buy (or how many you own).
- $1 minimum order, except that a whole position can always be sold.
- Buys round the cost up to the cent and sells round proceeds down, so
  rounding can never create money.
- `POST /portfolio/{id}/trade-preview` returns exactly what an order would
  do without placing it; the Portfolio page uses it as a live preview.

Options premiums are entered by hand (there's no free options price feed).
P/L is the account value (stocks at market price, options at cost, plus
cash) minus everything deposited.


The **AI** and **Growth** filters are rules, recomputed on every refresh
(edit them in `momentum/momentum_engine.py`):
- **AI:** $20B+ companies in Nasdaq's semiconductor, software, data-processing,
  computer-hardware and networking industries, plus a few anchors whose
  industry label hides their AI business (e.g. Amazon, filed under retail).
- **Growth:** $10B+ companies up at least 20% over 90 days and beating the
  S&P 500.

`/momentum` also returns `status.quotes_updated_at`, `status.price_source`
and `status.stale` (no successful price refresh in 10 minutes). The Market
page and the Portfolio trade form show the price time and source, and warn
when prices are stale.

## Data providers

Every paid provider is optional. With no keys set, the app uses only free
sources and behaves exactly as described above.

| Data | Default (free) | Optional upgrade | Key |
|---|---|---|---|
| Stock list, sector, market cap | Nasdaq screener | (kept: Massive has no sector field) | |
| Quotes (price, day change, volume) | Nasdaq screener, ~15 min delayed | Massive full-market snapshot | `MASSIVE_API_KEY` |
| 90-day price history | yfinance, ~3000 tickers in batches | Massive "grouped daily" (2 requests for the whole market) | `MASSIVE_API_KEY` |
| Company fundamentals + filings | SEC EDGAR (free, always on) | | `SEC_USER_AGENT` (contact, not a key) |
| News for sentiment | RSS feeds + company-name matching | Benzinga Newsfeed API (articles already tagged with tickers) | `BENZINGA_API_KEY` |

If a paid provider fails (bad key, plan limit, outage), the app logs a
warning and falls back to the free source for that refresh.

**Massive (formerly Polygon.io).** `momentum/market_data.py` calls
`https://api.massive.com` with a Bearer token:
- `GET /v2/snapshot/locale/us/markets/stocks/tickers` every 5 min for prices.
  Starter/Developer plans are 15-min delayed; Advanced/Business are real-time.
- `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` for the latest trading
  day and the day ~90 trading days earlier, stepping back over weekends and
  holidays. SPY stands in for the S&P 500 in relative strength.
- Differences from yfinance: Massive's `adjusted=true` adjusts for **splits
  only** (yfinance also adjusts for dividends), and the lookback start is a
  calendar approximation of 90 trading days. Momentum can differ by a
  fraction of a percent, more for high-dividend stocks.
- **Before going live with users**, request a Massive **business plan that
  permits displaying data to your users**. Individual plans are for personal
  use.

**SEC EDGAR.** `fundamentals/sec_edgar.py` reads the ticker→CIK map,
`companyfacts` (XBRL financials) and `submissions` (filing list). No key is
needed, but SEC requires a User-Agent with a contact email and allows at most
10 requests/second. Results are cached per company for 6 hours. Reporting is
normalized:
- For each metric, it uses whichever XBRL tag has the newest data, because
  companies rename tags (e.g. Apple's revenue).
- For each period, it keeps the most recently filed value, so restatements win.
- Annual vs quarterly is decided by the period length. Quarterly cash flow is
  year-to-date in 10-Qs, so cash flow is annual only.
- For IFRS filers (20-F), it uses `ifrs-full` tags and prefers USD values.
  These companies have no quarterly XBRL.
- Balance-sheet values more than two years old are dropped rather than shown
  as "latest".

**Benzinga.** `news_sentiment_scraper.py` calls
`https://api.benzinga.com/api/v2/news` (token in the query string, JSON via
the `accept` header). Each article's `stocks` list sets its tickers directly,
so it skips company-name matching. Claude's label cache is keyed on article
id + `updated` time, so an edited article is re-classified. If Benzinga
returns nothing or fails, the RSS feeds are used.

**Not built yet: streaming quotes.** A WebSocket client subscribed to
watchlists and holdings would make the trade screen real-time. It needs a
Massive plan with WebSocket access and a new dependency, so it waits until a
plan is chosen. Until then, prices refresh every 5 minutes and the UI shows
the price time and source and warns when prices are stale.

## Accounts and sign-in

Every page except `/login.html` requires sign-in; signed-out visitors are
sent to the sign-in page and returned where they were afterwards.

- **Email and password accounts** are stored in the `users` table of
  `paper_trading.db`. Passwords are hashed with scrypt and a per-user salt
  (`auth/passwords.py`); the plain password is never stored.
- **Sessions:** signing in creates a random token that the browser keeps in
  `localStorage` and sends as `Authorization: Bearer <token>`. The `sessions`
  table stores only its SHA-256 hash, with a 30-day expiry. Logging out
  deletes the row; changing the password deletes the user's other sessions.
- **New accounts** start with a $100,000 simulated portfolio.
- **Profile page** (`/profile.html`, from the account menu): change name and
  email, change password.
- **Demo account:** with `USE_DEMO_AUTH=true`, "Continue with demo account"
  signs in as the shared demo user (token `demo-token`).
- **Google sign-in (Firebase)** is still supported by the server: a Firebase ID
  token is accepted as a Bearer token. The pages don't have a Google button yet;
  see `auth/README.md`.
- Each account only ever sees its own portfolios, holdings and trades.

## API overview

Endpoints marked 🔒 require `Authorization: Bearer <token>`: a session token
from `/auth/login`, a Firebase ID token, or `demo-token` in demo mode. Each
user only sees their own portfolios.

| Method | Path | |
|---|---|---|
| POST | `/auth/register` | Create an email/password account (`name`, `email`, `password` of 8+ characters). Returns a session token |
| POST | `/auth/login` | Sign in (`email`, `password`). Returns a session token |
| POST | `/auth/logout` | End this session |
| GET | `/auth/me` 🔒 | The signed-in user's profile |
| PATCH | `/auth/me` 🔒 | Change name, and email for email/password accounts |
| POST | `/auth/password` 🔒 | Change password (`current_password`, `new_password`); signs out other sessions |
| GET | `/health` | Health check |
| GET | `/momentum?top=50&offset=0` | Tracked stocks ranked by momentum. Also takes `q` (search ticker/company/sector), `sector` (name, alias like `Tech`, or theme like `AI`) and `sort` (`momentum`, `market_cap`, `day_change`, `volume`) |
| GET | `/news-sentiment` | Latest headlines with sentiment; each has `affected`: the stocks it names (or, for sector-wide news, the largest stocks in those sectors) with a Good / Bad / Wait / No clear signal verdict from the headline's tone and the stock's 90-day trend. Plus `suggestions` (including `picks`: stocks with positive news on a rising 90-day trend, shown as "Good to invest in right now"): stocks and sectors in the news next to their current market condition. `refresh=true` refetches (at most once a minute; otherwise cached 5 min) |
| GET | `/price-history/{ticker}` | Daily closes for the 90-day momentum window (yfinance, cached 1 hour), used by the Market page chart |
| GET | `/fundamentals/{ticker}` | Revenue, net income, diluted EPS, operating cash flow (last 4 years), latest quarter, long-term debt, and links to recent SEC filings |
| GET | `/politicians` | Politicians with disclosed trades |
| GET | `/politicians/{name}/trades` | One politician's trades |
| POST | `/politicians/refresh` 🔒 | Pull fresh data from Quiver into the local cache |
| GET | `/portfolio` 🔒 | List your portfolios |
| POST | `/portfolio/default` 🔒 | The portfolio the pages use: your most recent one, created with $100,000 on first visit |
| POST | `/portfolio` 🔒 | Create a portfolio (`name`, `starting_capital`) |
| POST | `/portfolio/{id}/build` 🔒 | Build it from the top momentum names among the 500 largest stocks |
| GET | `/portfolio/{id}` 🔒 | Portfolio summary, stocks valued at live prices, P/L vs deposits |
| POST | `/portfolio/{id}/mark-to-market` 🔒 | Update with current prices |
| POST | `/portfolio/{id}/cash` 🔒 | Add cash (`amount` > 0) |
| POST | `/portfolio/{id}/trade-preview` 🔒 | What a market order would do (shares, cost, cash after, or why it's rejected); places nothing |
| POST | `/portfolio/{id}/trade-stock` 🔒 | Market order: `ticker`, `side` (`BUY`/`SELL`) and either `quantity` (shares) or `amount` (dollars) |
| POST | `/portfolio/{id}/trade-option` 🔒 | Buy/sell a call or put |
| GET | `/portfolio/{id}/history` 🔒 | Daily portfolio value history (for the performance chart) |
| GET | `/portfolio/{id}/trade-history` 🔒 | Trade history |

Example (demo mode):

```bash
# Sign in (or use "Authorization: Bearer demo-token" with USE_DEMO_AUTH=true)
TOKEN=$(curl -s -X POST http://localhost:8000/auth/register -H "Content-Type: application/json" \
  -d '{"name": "Ada", "email": "ada@example.com", "password": "correct-horse"}' | python -c "import json,sys; print(json.load(sys.stdin)['token'])")
AUTH="Authorization: Bearer $TOKEN"

curl -X POST http://localhost:8000/portfolio -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Portfolio", "starting_capital": 1000}'

curl -X POST http://localhost:8000/portfolio/1/build -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"max_positions": 5}'

# Invest $100 in AAPL at the market price (fractional shares)
curl -X POST http://localhost:8000/portfolio/1/trade-stock -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"ticker": "AAPL", "side": "BUY", "amount": 100}'

curl http://localhost:8000/portfolio/1 -H "$AUTH"
```

## Running pieces standalone

```bash
python momentum/momentum_engine.py                  # default universe
python momentum/momentum_engine.py AAPL MSFT NVDA   # custom tickers
python news_sentiment/news_sentiment_scraper.py
python politician_trades/politician_trades.py       # needs QUIVER_API_KEY
```

## Tests

```bash
pytest
```

## Known limitations / next steps

See [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) for the full roadmap.

- **Accounts** have no email verification or password reset (the app
  sends no email), and no rate limiting on sign-in. Add both before
  exposing the app beyond your own machine.
- **Market data** defaults to free, unofficial sources: Nasdaq's screener
  API and yfinance. Prices are ~15 minutes delayed and either source can
  change or rate-limit without notice. Set `MASSIVE_API_KEY` to use Massive
  instead (see [Data providers](#data-providers)). Streaming quotes are not
  built yet.
- **News sentiment** caches Claude's label per article, but only in memory,
  so a restart re-classifies current headlines. Without `BENZINGA_API_KEY`,
  headlines are matched to companies by name, which misses articles that only
  use a ticker or nickname. If the feed has no relevant headlines, it returns
  a curated demo fallback.
- **Paper trading** buys whole shares only; leftover cash stays as cash.
- **No database migrations** — the SQLite schema is created with
  `CREATE TABLE IF NOT EXISTS` on startup; moving to Postgres or a cloud DB
  will need a migration tool (Alembic, etc.).
- **No scheduled jobs** — momentum and news update only when the endpoints
  are called. A deployment should add a scheduler to refresh data.
