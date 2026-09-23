# Project Status Update

## Status: Stable MVP / Ready for next phase

This project has reached a working multi-page investment dashboard with live sentiment monitoring, portfolio tracking, paper-trading flows, and a polished UI. The app is stable enough for product-level review and for planning the next release milestone.

## Completed work

### 1. Multi-page dashboard structure
- Overview page, market page, portfolio page, sentiment page, and options page are all presented as distinct views linked together.
- Navigation and profile controls are wired into the app shell.
- Theme switching is supported with light and dark mode.

### 2. Portfolio management
- Portfolio creation and loading from the API.
- Starting capital and user-defined portfolio names.
- Remaining cash balance display.
- Total invested value summary.
- Number of stocks currently held.
- Sector allocation view with percentage breakdowns.
- Add-cash flow to increase investable capital.
- Trade history display for portfolio activity.

### 3. Paper trading and option flows
- Simulated trading engine with holdings and transaction tracking.
- Option trade and history support.
- Cash balance and trading limits are recorded in the portfolio state.
- Portfolio summary endpoints now return richer investment metrics.

### 4. News sentiment and live feed
- RSS-based sentiment collection from multiple sources.
- Latest news ordering with newest-first display.
- Source and sentiment filtering.
- Pagination for long news feeds.
- Auto-refresh flow for sentiment data updates.
- Recent history retention to support follow-up analysis.
- Scraper fallback logic to avoid stale source behavior.

### 5. Visual polish and UX improvements
- Apple-inspired dashboard styling and layout direction.
- Distinct color treatment for light and dark mode.
- Improved contrast and card differentiation.
- Hover and active motion effects on interactive buttons.
- Cleaner portfolio and sentiment card layouts.

### 6. Validation coverage
- Automated regression tests for sentiment and paper-trading flows are in place.
- Latest verification run: 31 tests passed (2026-09-23).

## Current halt / release checkpoint

Before moving into the next feature layer, the following areas should be treated as the release gate:

1. Confirm browser-level flow for portfolio, sentiment, and options actions.
2. Validate the live feed refresh behavior in a running app session.
3. Verify trade history and balance updates remain consistent over several actions.
4. Make sure the auto-refresh cycle is not overloading the scraping layer.

### Browser check results (2026-09-23)

Driven in headless Chrome against a running app, with demo auth, on a copy of an existing local database.

1. **Portfolio, options, billing:** pass, after three fixes:
   - Option trades returned a 500 on databases created before options support (old `UNIQUE(portfolio_id, ticker)` constraint on holdings). The engine now rebuilds the legacy table on startup.
   - The portfolio page crashed partway through rendering (it still wrote to the removed "Stocks" stat card), so holdings, sectors, performance and buying power stayed empty.
   - A slow live-price lookup could overwrite a price the user had typed, so trades executed at a different price than entered.
2. **Live feed refresh:** pass. The sentiment page refreshes every 20 seconds.
3. **Trade history and balances:** pass. Buy, sell and add-funds balances matched expected values exactly in the UI and the API.
4. **Scraping load:** fixed (2026-09-23). `/news-sentiment` is cached for 5 minutes and Claude classifications are cached per headline, so Claude runs at most 12 times an hour regardless of open tabs. Without `ANTHROPIC_API_KEY`, labels still come from a keyword heuristic and are often wrong.

### Fixed after the browser check (2026-09-23)

Architecture review:
- Concurrent trades could overspend cash (shared SQLite connection, check-then-write). Engine methods now run under one lock.
- The client set the trade price. Stock trades are now rejected if more than 5% from the latest market close.
- Stock buys stored sector "Unknown" (AAPL showed "Unknown"). The real sector is recorded and tickers are uppercased; existing Unknown holdings update on the next buy.
- `sentiment_history.json` is runtime data: gitignored, and tests write to a temp file instead of the real one.
- The six copies of the frontend fetch helper are one shared `frontend/api.js`.
- `api/main.py` uses package imports instead of `sys.path` hacks.
- Cash balances are rounded to cents on every write, so float error can't accumulate.

UI and security:
- Redesigned UI: token-based light/dark themes, mobile nav strip, reduced-motion/transparency/contrast support. This fixed the low-contrast BUY/SELL badges in light mode.
- The billing page profile menu had no click handler; it opens now.
- The market page inserted RSS headlines as raw HTML (XSS from a malicious feed). Feed text is now escaped everywhere, and "Read article" links only allow http(s) URLs.
- Headlines showed HTML entities raw (e.g. `S&amp;P`). The scraper now strips tags and decodes entities before the frontend escapes them.

Other things seen, not yet fixed:
- The frontend always sends `demo-token`, so real Firebase sign-in isn't wired into the pages. `getAuthToken()` in `frontend/api.js` is the one place to change.
- Some older option trades in existing databases have an empty ticker (shown as "N/A").
- Option premiums are not validated against a market price (stock prices are).
- The trade lock only works with a single server process; multiple uvicorn workers would need per-request connections or Postgres.
- Money is stored as `REAL` rounded to cents; switch to integer cents before adding fees, tax lots, or fractional shares.

## Next major items to complete

### High priority
- AI autopilot investing layer for automatic buy/sell decisions based on signal quality.
- More explicit risk controls and allocation guardrails.
- Better user authentication and portfolio ownership model.
- Real-time market data provider integration for more robust pricing.

### Medium priority
- Enhanced charting for portfolio performance and sector trends.
- Holdings drill-down with per-stock performance and trade details.
- Scheduler for sentiment and momentum refresh jobs.
- Better historical retention and analytics storage.

### Long-term product work
- Multi-user production deployment.
- Cloud database and background jobs.
- Observability, logging, and monitoring.
- Production-grade security and API hardening.

## Recommendation

The project is in a good working state for a demo and internal review, but it should pause before autopilot expansion until the current live feed, portfolio, and trading flows are validated in a full browser session. That is the recommended stabilization checkpoint before adding the automated investment layer.
