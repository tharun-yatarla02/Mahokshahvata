# Project Status

_Last updated: 2026-09-24 (after the Claude structured-outputs update)_

Picking this up? Read [HANDOFF.md](HANDOFF.md) first (setup, architecture,
gotchas, and a starting point for each open task).

## Where we are

**Feature-complete demo, running locally.** All six pages work end to end with
paper money, live (~15 min delayed) data for the 3,000 largest US stocks, and
news-driven suggestions. 82 automated tests pass, and every page and button was
checked in a browser (light, dark, phone). It is **not yet a public website**:
sign-in is demo-only and there's no hosting setup.

## What's new (Sep 23–24, 2026)

For anyone catching up after the Sep 23 evening push. Details are in the
commit messages (`git log`).

- **Market data:** the app tracks the 3,000 largest US stocks (was 20), refreshed in the background; Market page has search, sort, sector filters and paging.
- **Paper-trading rules:** stock trades fill at the market price only; buy by shares or by dollar amount (fractional shares); cash/ownership checks with a "you can get N shares" caution and Max button; $1 minimum; live order preview.
- **Real P/L:** measured against money deposited; holdings valued at live prices; options kept at cost. Existing portfolios were backfilled from their trade history.
- **Portfolio page:** add-funds moved to Billing; fake trigger/limit field removed; all sectors filterable; stable weights; market-value sector breakdown; real risk dial; real daily value chart.
- **Options page:** redesigned layout, cost preview, open positions with one-click Close, options-only history, date-picker expiry.
- **Sentiment page:** Claude-classified headlines (structured outputs, Haiku 4.5) matched to the 1,000 largest companies; suggested stocks and sectors shown with their present market condition.
- **New users:** every page gets or creates the user's $100,000 portfolio (no more dead ends).
- **Setup:** keys go in a gitignored `.env` file, loaded on startup. Each developer needs their own `ANTHROPIC_API_KEY` there for Claude sentiment.
- **Fixes:** Infinity/NaN inputs, portfolio crash after mark-to-market, market data wiped by a failed download, 3 MB sentiment payload, demo-auth impersonation, unescaped user text, and more (see commits `a925a59`, `f6c8172`, `558d3ea`).

## What works today

| Area | What a user can do |
|---|---|
| **Overview** | Account value, P/L, risk level, holdings; live Top Movers per sector (AI / Tech / Health / Energy) linking to Market |
| **Market** | Browse, search, sort and filter the 3,000 largest US stocks (momentum, today's move, market cap, volume); sector and theme filters; stock detail with company, market cap, related news |
| **Portfolio** | Holdings valued at live prices; P/L against money deposited; sector breakdown and filters; real daily value chart; market-order trading by shares or dollars (fractional to 6 decimals, $1 minimum, cash/ownership checks with a "how much you can get" caution and Max button); trade history |
| **Sentiment** | Latest headlines from 4 sources, classified by Claude Haiku 4.5 with structured outputs (keyword fallback without an API key); suggested stocks and sectors with their present market condition; filters, search, pagination, manual refresh |
| **Options** | Buy/close calls and puts with a cost preview, open positions with one-click Close, options trade history |
| **Billing** | Add funds; cash / allocation / risk views of the account |

Behind the scenes:
- **Market data:** background service tracks the top 3,000 US stocks (Nasdaq screener every 5 min, Yahoo history every 6 h), cached to disk, survives outages.
- **Paper-trading rules:** market price only, fractional shares, rounding that can't create money, no overspending (thread-safe engine), P/L measured against deposits.
- **Safety:** input validation (no Infinity/NaN, ticker/expiry checks), escaped output, demo auth limited to the single demo token, CORS locked to localhost.

## What's missing

### Needed before it can go live as a website
1. **Real sign-in.** The backend verifies Firebase tokens, but every page sends `demo-token`. Needs a sign-in screen and Firebase config in `frontend/api.js` (`getAuthToken()`); "Logout" and the "Account" button are placeholders until then.
2. **Hosting.** No Dockerfile, deploy config, or production settings (Firebase service account, `ALLOWED_ORIGINS`, `ANTHROPIC_API_KEY`). The app assumes one server process; multiple workers need Postgres or per-request DB connections.
3. **Real-time data (optional for a demo).** Prices come from free, unofficial sources, ~15 min delayed. A paid provider (Polygon, Finnhub, Alpaca) would slot into `momentum/market_data.py`.

### Still showing placeholder content
4. **Market page stock chart is fabricated** (fixed percentages of the current price). Replace with real price history or remove.
5. **Hardcoded labels:** Overview "Signal quality: Strong" and the static "AI Market Signal" card (Technology Bullish / Healthcare Stable / Energy Cautious); Portfolio "Balanced Growth"; Options "Defined risk"; Market "Scanning in real time". These should be computed (e.g. from the sentiment sector suggestions) or removed.
6. **Stock detail "Related news"** shows the three newest unrelated headlines when none mention the stock.

### Backend features with no screen yet
7. **Build portfolio from momentum** (`POST /portfolio/{id}/build`).
8. **Multiple portfolios:** the API supports several per user; pages always use the most recent and there's no create/switch UI.
9. **Politician trades** (`/politicians/*`, needs `QUIVER_API_KEY`).

### Product roadmap (not started)
10. **AI autopilot** investing layer (automatic buy/sell from signals), with explicit risk guardrails.
11. **Options pricing:** premiums are typed by hand (no free options price feed) and there's no 100-share contract multiplier.
12. Watchlists and price alerts; per-stock performance drill-down.
13. **Own sentiment model (optional):** train a local model so sentiment runs free and offline. The scaffolding exists (`news_sentiment/model/`, `SENTIMENT_BACKEND=local`) but nothing is trained and PyTorch isn't installed. Plan: save every Claude-labeled headline as training data, fine-tune DistilBERT on public financial datasets plus those headlines, measure agreement with Claude on held-out headlines, then switch via `.env`.

### Housekeeping
- One test (`tests/test_momentum_sector_lookup.py::test_rank_momentum_uses_sector_lookup`) downloads live prices, so the suite fails offline.
- Money is stored as `REAL` rounded to cents; move to integer cents before adding fees or tax lots.

## Suggested order
1. Replace the placeholder content (items 4–6): small, and it's what a reviewer notices first.
2. Real sign-in + hosting (items 1–2): the step from demo to website.
3. Portfolio build and multiple portfolios UI (items 7–8).
4. Autopilot (item 10), once the above is stable.
