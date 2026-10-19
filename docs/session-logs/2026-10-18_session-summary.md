# Session summary — Oct 17–18, 2026

What happened in this working session, in order, with the decisions made and what
is still open. The commit-by-commit list is in the automatic session logs next to
this file; this page explains them.

## 1. Checks and quick wins (Oct 17)

- **README check.** Everything the README describes exists: every file in the
  layout, all API endpoints, all environment variables. Two stale lines in "Known
  limitations" were corrected (whole shares only applies to the momentum build;
  market data and the autopilot do run on schedules).
- **Sign-in rate limiting** (backlog item 1): 5 failed sign-ins per email or 20
  per IP in 15 minutes return 429 with `Retry-After` (`auth/rate_limit.py`).
- **Hardcoded labels replaced with live values** (backlog item 6): news tone and
  "Sectors in the News" on the Overview, market open/closed on Market, largest
  sector on Portfolio, open positions on Options.
- **Deployed `v2`**, which also brought the Oct 10 autopilot to the live site.

## 2. Flaw audit and button click-through (Oct 17)

Fixed:
- **Option premiums could create unlimited cash** (sell at any typed price). Now
  bounded by intrinsic value, the stock price and the strike.
- **`mark-to-market` trusted prices sent by the browser** and wrote them into the
  value history the autopilot's daily-loss limit reads. Now uses tracked prices.
- **The daily-loss limit compared against a stale "last close"** (only saved when
  someone opened the Portfolio page). The scheduler now saves each autopilot
  portfolio's value after the close.
- **The Overview page could be cached by browsers** after a deploy.
- **An unescaped sector name** on the Market page.
- **Options form example was invalid** under the new premium bounds; it now
  suggests a strike and premium from the live price and shows the allowed range.
- **Clicking a one-letter ticker (e.g. C) on Sentiment filtered nothing**; it now
  matches tagged tickers exactly.

Every button on every page was clicked in a headless browser: no JavaScript
errors or failed requests remained.

## 3. Autopilot questions answered

It is rules-based automation, not an AI model: momentum and relative strength
against the S&P 500 pick the stocks, news labelled by Claude filters them, and
guardrails (position count, per-stock cap, cash reserve, stop-loss, daily-loss
kill switch) limit the amounts. Training a model to make the decisions is possible
(a LightGBM ranker with a walk-forward backtest was suggested as the first step);
not started.

## 4. Milestone 4: all state off the VM (Oct 17–18)

Decisions: Cloud SQL for PostgreSQL (the rubric's private IP and `CREATE TABLE`
requirements rule out Firestore), profile photos as the upload feature, VM
downtime allowed, and the user's `.env` never read or uploaded.

Built and deployed:
- **Database:** Cloud SQL PostgreSQL 16 `mahokshahvata-db`, private IP
  `10.27.48.3` only, TLS required, daily backups (7 kept), zone `us-central1-c`.
  Tables from `db/migrations/001_initial.sql` (9 `CREATE TABLE`), applied by
  `db/migrate.py`. The trading engine was rewritten from Firestore to SQL with the
  same API; trades lock the portfolio row.
- **Data move:** 39 of 39 rows copied from Firestore, every table matching
  (`docs/deploy/row_counts.txt`), ids kept, one transaction. A maintenance page
  showed during the move.
- **Files:** private bucket `project-262a2026-879b-4b49-88e-app-data` for the market
  cache, sentiment history and the new profile photos (`/auth/me/photo`).
- **Secrets:** Secret Manager holds `db-password`, `db-admin-password` (the VM
  can't read it) and `anthropic-api-key` (value added by the user in the console).
  Passwords were generated and piped straight in; nobody has seen them.
- **Identity:** dedicated service account `mahokshahvata-vm` with roles granted
  per resource; the VM was switched to it. The Firebase Admin key file was shredded
  from `/opt/mahokshahvata`.
- **Google sign-in** moved to the team's own Firebase project `mahokshahvata`
  (the old `mahokshahvata-ae5f9` belongs to another account). Tokens are checked
  with the project id only. The user confirmed sign-in lands in the same account.
- **Images:** `v3` (Milestone 4), then `v4` (new sign-in project), live at
  http://34.122.164.209.

Fixes made along the way: optional secrets the VM can't see are reported by
Secret Manager as "permission denied" and are now skipped; a quoting bug in
`docker_deploy.sh` that crashed `gcloud` (the remote script now goes over stdin);
the infra script now gives the built-in `postgres` admin a random password.

Deliverables produced:
- `docs/deploy/MILESTONE4.md`: before/after, identity and network, rubric
  evidence, row counts, written analysis, screenshot checklist.
- `docs/deploy/Milestone4_State.pptx`: the 2 slides, with speaker notes.
- `docs/deploy/Milestone4_Written_Analysis.pdf`: the 1-page written analysis.
- `docs/deploy/MILESTONE4_VIDEO.md`: the minute-by-minute video script with every
  link and command.
- The user took the 6 screenshots.

## Still open

- **Old Milestone 2 folder on the VM:** `/home/siddhardhaanagani/app` still holds a
  copy of the old Firebase key file and an Oct 1 SQLite file. Unused (its services
  are disabled), but it contradicts "no key files on the VM". The user chose to
  leave it for now; say the app and its container have no key files when presenting.
- **Revoke the old Firebase key** in project `mahokshahvata-ae5f9` (needs its owner).
  It can read only the old Firestore copy, not live data.
- **Video:** record, upload to OneDrive, share with the three graders, submit the
  link in Canvas.
- **Optional:** ship container logs to Cloud Logging; remove the old default
  service account's leftover image-pull access.
- **Backlog after the milestone:** portfolio switcher and "Build from momentum"
  button (items 7–8), watchlists (10), options contract multiplier (11).
