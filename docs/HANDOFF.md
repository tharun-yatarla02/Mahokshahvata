# Developer Handoff Guide

Everything you need to pick up this project from git: set it up, understand
how it fits together, avoid the known traps, and start on the next task.

- **What the app is and its API:** [README.md](../README.md)
- **What's done and what's left:** [PROJECT_STATUS.md](PROJECT_STATUS.md)
- **This file:** how to work on it.

_Last verified: 2026-09-24, from a fresh `git clone` on macOS with Python 3.11
(install ~20 s, 82 tests pass, all pages load)._

---

## 1. Get it running (about 5 minutes)

```bash
git clone https://github.com/tharun-yatarla02/Mahokshahvata.git
cd Mahokshahvata

python3 -m venv .venv                 # Python 3.11+ (3.10 minimum)
source .venv/bin/activate
pip install -r requirements.txt

# Settings live in .env (gitignored, loaded automatically on startup)
cat > .env <<'EOF'
USE_DEMO_AUTH=true
ANTHROPIC_API_KEY=sk-ant-your-own-key
EOF

pytest                                 # expect: all tests pass
uvicorn api.main:app --reload          # then open http://localhost:8000
```

**What to expect on the very first start:**
- The Market page says *"Loading market data…"* for about **2 minutes** while
  it downloads price history for 3,000 stocks. After that it's cached in
  `momentum/market_cache.json`, and restarts are instant.
- The first page you open creates a $100,000 demo portfolio in Firestore
  (the local emulator when developing; see docs/DEVELOPMENT.md).
- The Sentiment page uses Claude if `ANTHROPIC_API_KEY` is set; without it,
  headlines are labeled by a simple keyword fallback (works, less accurate).
  Use **your own** API key; never commit it.

**Troubleshooting**

| Symptom | Cause / fix |
|---|---|
| Every API call returns 401 | `USE_DEMO_AUTH=true` is missing from `.env` (the pages send `demo-token`) |
| Sentiment reasons say "Heuristic sentiment fallback" | No valid `ANTHROPIC_API_KEY`. Check `.env` has exactly one key on one line, then restart the server (`.env` is read at startup; `--reload` doesn't re-read it) |
| Market page stuck on "Loading market data" for > 5 min | Nasdaq or Yahoo unreachable. `GET /momentum?top=1` shows `status.last_error`; it retries automatically |
| `ModuleNotFoundError` when running a file directly | Run from the project root; imports are package-style (`from momentum.market_data import ...`). `pytest.ini` sets this up for tests |
| One test fails offline | `test_rank_momentum_uses_sector_lookup` downloads live prices; everything else runs offline |

---

## 2. Project map

```
api/main.py                  FastAPI app: every endpoint, request validation, serves the frontend.
                             Also: .env loader, trade planning (_plan_order), sentiment cache.
paper_trading/
  paper_trading_engine.py    Paper-trading engine (Firestore): users, sessions, sign-in history,
                             portfolios, trades, options, P/L, daily snapshots. Thread-safe (@_locked).
momentum/
  market_data.py             Background service: top-3,000 US stocks, quotes + history, disk cache.
  momentum_engine.py         Momentum math, sector/theme filters (AI, Tech, ...), stock search.
news_sentiment/
  news_sentiment_scraper.py  RSS fetch -> match headlines to companies -> classify (Claude
                             structured outputs / keyword fallback / local model).
  suggestions.py             Company-name aliases for matching; stock & sector suggestions.
  company_tickers.csv        Curated company names -> tickers (case-sensitive matching).
  model/                     Local sentiment model scaffolding (not trained yet).
politician_trades/           Congressional trades via Quiver (backend only, no page yet).
auth/firebase_auth.py        Firebase token verification + demo mode.
frontend/
  *.html                     One file per page (HTML + inline script). No build step.
  api.js                     Shared helpers every page loads: apiFetch, getPortfolioId,
                             escapeHtml, safeUrl, errorMessage. Auth token lives here.
  ticker-picker.js           Stock search dropdown (Portfolio, Options).
  styles.css                 Design system: all colors are CSS variables (light/dark themes).
tests/                       pytest suite (see section 5).
docs/                        PROJECT_STATUS.md, this guide, session logs.
```

---

## 3. How it works

### Market data (`momentum/market_data.py`)
- **Stock list + quotes:** Nasdaq's public screener, one request for all US
  listings, every **5 min**. Keeps the 3,000 largest common stocks (drops
  preferreds, warrants, units, notes; keeps ADRs). Prices are ~15 min delayed.
- **History:** Yahoo (yfinance) daily closes every **6 h**, used for the
  90-day start price. Momentum = latest quote vs. 90 trading days ago.
- **Failure handling:** if a download returns under half the stocks, the old
  data is kept and it retries in 15 min. Everything is cached to disk.
- API: `market_data.snapshot()` (ranked rows), `lookup(ticker)`, `status()`.

### Paper trading (`paper_trading/paper_trading_engine.py` + `api/main.py`)
- **Market orders only.** `_plan_order()` in `api/main.py` gets the market
  price (`_market_price()`: tracked quote, else a cached Yahoo lookup), works
  out shares and cost, and checks cash/ownership. The preview endpoint and
  the real trade both use it, so they always agree. Client prices are ignored.
- **Money rules** (engine helpers `round_shares`, `order_value`,
  `shares_for_amount`): shares to 6 decimals, buys round cost **up** to the
  cent, sells round proceeds **down**, $1 minimum except selling a whole
  position. Use these helpers for any new money math.
- **P/L** = (cash + stocks at live price + options at cost) − `contributed_capital`
  (starting capital + deposits). `get_portfolio_summary(..., price_for=...)`
  does the valuation; the API passes live prices.
- **Snapshots:** viewing a portfolio records one value per day in
  `daily_snapshots`; the Portfolio chart plots them.
- **Thread safety:** every public engine method holds one lock (`@_locked`).
  Safe for one server process, not for multiple workers.
- **Migrations run automatically** on startup (`_migrate_schema`): adds
  columns, rebuilds the old holdings table, backfills `contributed_capital`.
  There is no migration tool; add new steps there and keep them idempotent.

### Sentiment (`news_sentiment/`)
1. `fetch_articles()` reads 4 RSS sources (freshest feed per source), cleans
   HTML, sorts by publish time.
2. `match_tickers()` tags headlines with companies: curated CSV names + the
   1,000 largest companies' names (case-sensitive) + explicit mentions like
   `$NVDA` / `(NYSE: TGT)`. Everyday-word names (Target, Shell, Popular, ...)
   are in `AMBIGUOUS_NAMES` and only match via explicit tickers. Headlines
   with no company but a macro theme (war, rates, oil...) map to sectors.
3. `classify_batch()` sends 10 headlines per call to **Claude Haiku 4.5**
   with **structured outputs** (`client.messages.parse` + Pydantic schema):
   always valid JSON, sentiment limited to Positive/Negative/Neutral.
   Results are cached per headline; failures fall back to keywords.
4. `/news-sentiment` caches headlines for 5 min (manual refresh: once a
   minute) and rebuilds `suggestions` (suggestions.py) on every request with
   live prices.

### Frontend
- Plain HTML/JS, no framework or build. Each page's script is inline.
- **Every page must load `/api.js` first.** Use `apiFetch()` for API calls,
  `getPortfolioId()` to get the user's portfolio, and `escapeHtml()` /
  `safeUrl()` for anything inserted with `innerHTML`.
- **Adding a page or static file:** FastAPI serves each file through its own
  route in `api/main.py` (see `/market.html`, `/api.js`). Add a route too.
- **Styling:** use the CSS variables (`--surface`, `--text`, `--muted`,
  `--pos`, `--neg`, `--accent`, ...) instead of hex colors so both themes work.

---

## 4. Files that are NOT in git (created locally)

| File | What | Safe to delete? |
|---|---|---|
| `.env` | Your keys and settings | No (recreate it) |
| `momentum/market_cache.json` | Market data cache | Yes (re-downloads, ~2 min) |
| `news_sentiment/sentiment_history.json` | 5-day headline history | Yes |
| `politician_trades/politician_trades.db` | Quiver cache | Yes |

Your database is yours alone; git never shares portfolio data.

---

## 5. Tests and checking your work

```bash
pytest                          # full suite (~3 s)
pytest tests/test_api_hardening.py -k trade   # a subset
```

| Test file | Covers |
|---|---|
| `test_api_hardening.py` | API validation, market orders, previews, auth, CORS, sentiment cache, .env loader |
| `test_paper_trading_options.py` | Engine: trades, options, P/L, deposits, snapshots, migrations |
| `test_market_data.py` | Universe filtering, momentum, cache, outage handling, search |
| `test_news_suggestions.py` | Company matching, suggestions, sector conditions |
| `test_sentiment_live_feed.py` | Feed parsing, timestamps, Claude structured output, fallbacks |
| `test_demo_auth.py` | Demo auth rules |
| `test_momentum_sector_lookup.py` | Sector filters (one test needs internet) |

**Rules we've followed:** every bug fix gets a test that fails without the
fix; tests use temp databases (never the real one) and fake the network.

**Before pushing UI changes,** open every page you touched in both themes
(Dark/Light toggle) and at phone width, click the buttons, and check the
browser console for errors.

---

## 6. Working with git

```bash
git pull                              # start from the latest
git checkout -b your-feature          # optional: work on a branch
# ...change code, add tests...
pytest
git add <files>                       # never .env or *.db (already gitignored)
git commit                            # message: what changed and why
git push
```

- Commit messages in this repo explain *what* and *why* per area; `git log`
  is the detailed change history.
- `.claude/settings.json` has a Claude Code `SessionEnd` hook that writes a
  summary to `docs/session-logs/`. It only runs inside Claude Code; commit
  or ignore those logs as you like.

---

## 7. Gotchas

- **Restart the server after editing `.env`.** `--reload` only watches Python files.
- **Don't trust prices from the browser.** Stock trades must go through
  `_plan_order()`; the engine assumes the price it's given is the market price.
- **Don't insert data into the page without `escapeHtml()`** (tickers, names
  and headlines come from users and feeds).
- **Single process only.** Don't run uvicorn with `--workers > 1`; the
  engine lock isn't shared across processes.
- **Options aren't market-priced.** Premiums are typed in and valued at cost;
  there's no ×100 contract multiplier.
- **Sectors come from Nasdaq's classification** (e.g. Thermo Fisher is
  "Industrials", Berkshire has none).
- **zsh tip:** don't name a shell loop variable `path`; it overwrites `PATH`.

---

## 8. Where to pick up

Full list with priorities: [PROJECT_STATUS.md → What's left](PROJECT_STATUS.md#whats-left).
Starting points for the next tasks:

| Task | Where to start |
|---|---|
| **Real stock chart** on the Market page (currently fabricated) | `renderMarketDetail()` in `frontend/market.html` builds `chartSeries` from fixed percentages. Add an endpoint returning daily closes (yfinance, cached like `_market_price`) and plot them |
| **Replace hardcoded labels** ("Signal quality: Strong", static "AI Market Signal" card, "Balanced Growth", "Defined risk", "Scanning in real time") | Search those strings in `frontend/*.html`. The sector signals already exist in `/news-sentiment` → `suggestions.sectors` |
| **Related news** shows unrelated headlines when none match | `getRelatedNews()` in `frontend/market.html` (the `headlineMatches.length ? ... : ...` fallback) |
| **Real sign-in** | `getAuthToken()` in `frontend/api.js` (the only place the token is set) + the Firebase snippet in `auth/README.md`; turn off `USE_DEMO_AUTH` |
| **Portfolio builder / multiple portfolios UI** | Endpoints exist: `POST /portfolio/{id}/build`, `GET/POST /portfolio`. Pages pick the portfolio via `getPortfolioId()` in `frontend/api.js` |
| **Own sentiment model** | `news_sentiment/model/` + `SENTIMENT_BACKEND=local`. Plan in PROJECT_STATUS item 13. Note `classify_all_local()` reloads the model on every call; cache it |
| **Hosting** | Needs a Dockerfile/deploy config, production `.env` (Firebase credentials, `ALLOWED_ORIGINS`, API key), and one server process (or Postgres) |
