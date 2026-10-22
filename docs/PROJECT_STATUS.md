# Project Status

_Last updated: 2026-10-18 (Milestone 4 done: state off the VM, sign-in on the team's Firebase project, v4 live)_

Picking this up? Read [HANDOFF.md](HANDOFF.md) first (setup, architecture,
gotchas, and a starting point for each open task).

## Where we are

**Live website with no state on the VM.** The app runs at
http://34.122.164.209 on a Google Cloud VM (firewalled to the team's IPs). Its
data is in Cloud SQL for PostgreSQL on a private IP, files are in a private
Cloud Storage bucket, and keys are in Secret Manager, all reached as the VM's
own service account; deleting the VM would cost only downtime (see
[deploy/MILESTONE4.md](deploy/MILESTONE4.md)). It tracks the 3,000 largest US
stocks (~15 min delayed), reads live news with Claude, and lets each user
paper-trade stocks and options. The same image (`app:v5`) runs on laptops with
`docker compose up` (with a local Postgres) and on the VM. 159 automated tests
pass, and GitHub Actions runs them against Postgres plus a Docker build on every push.

## What works today

| Area | What a user can do |
|---|---|
| **Sign-in** | Create an account with email and password, or Continue with Google. One account per email; 30-day sessions; clear messages for a taken email or a wrong password |
| **Profile** | Change name; email and password for email accounts (changing the password signs out other devices); upload a profile photo (stored in the bucket, shown in the account menu) |
| **Overview** | Account value, P/L, risk level, holdings; live Top Movers per sector (AI / Tech / Health / Energy) |
| **Market** | Search, sort and filter the 3,000 largest US stocks; stock detail with a real 90-day price chart, SEC fundamentals, related news, and **Explain this stock** (a plain-English summary by Claude, or by rules without a key) |
| **Portfolio** | Holdings at live prices; P/L against money deposited; sector breakdown; daily value chart; market-order trading by shares or dollars (fractional, $1 minimum, cash checks); trade history |
| **Sentiment** | Headlines from 4 RSS sources (or Benzinga), labelled by Claude Haiku 4.5; each headline tagged with the stocks it names (48 of 49 found on a live test, no wrong tags); Good / Bad / Wait verdicts; "Good to invest in right now" picks |
| **Options** | Buy and close calls and puts with a cost preview; open positions; options history |
| **Billing** | Add funds; cash / allocation / risk views |
| **Autopilot** | Rules-based paper trading of a portfolio: preview planned trades with reasons, run now or once each weekday, guardrails and a daily-loss kill switch; touches only stocks it bought |

Behind the scenes:
- **Hosting:** Docker image (non-root, health-checked, no secrets inside) in
  Artifact Registry; the VM runs it with Docker Compose behind Caddy. Deploy or
  roll back with `TAG=<version> ./deploy/docker_deploy.sh`. See
  [deploy/MILESTONE3.md](deploy/MILESTONE3.md) and [deploy/MILESTONE4.md](deploy/MILESTONE4.md).
- **Data:** Cloud SQL PostgreSQL 16 (`mahokshahvata-db`, private IP only, TLS,
  daily backups), tables from `db/migrations/`; files in
  `gs://project-262a2026-879b-4b49-88e-app-data`; secrets in Secret Manager.
  Firestore still holds the Milestone 3 copy, unused.
- **Cost controls:** $50 budget with alerts at 10/20/25/50/100% plus a $100
  budget; free-tier e2-micro; registry cleanup policy; capped container logs.
  Current spend is $0.00 after the free tier.
- **Team workflow:** both of us push to `master`; a Claude Code hook warns
  before committing or pushing when the other has pushed (local setting, not
  committed).

## What's left

### Security and accounts
1. ~~**Sign-in rate limiting.**~~ ✅ Done Oct 17: 5 failures per email or 20
   per IP in 15 minutes return 429 (`auth/rate_limit.py`, in memory).
2. **Forgot password (parked).** Needs a way to send email. Firebase can send
   reset emails only for accounts it manages, and our email/password accounts
   are managed by the app, so this needs either moving them to Firebase Auth or
   an email service (SendGrid, Gmail SMTP).
3. **Email verification** on registration (same email dependency as item 2).
   It would also let an account joined to Google keep its password.
4. **Sign-in history is kept forever** (`auth_events`, with IP and browser).
   Add a retention limit, e.g. 90 days.
5. **Duplicate emails from before the one-account rule** have not been checked
   on the real database: run `python deploy/find_duplicate_emails.py` with the
   Firebase Admin key.

### Placeholder content
6. ~~**Hardcoded labels**~~ ✅ Done Oct 17: Overview shows the news tone and
   "Sectors in the News" (tone + median momentum) from `/news-sentiment`;
   Market shows open/closed from the NYSE calendar; Portfolio its largest
   sector; Options its open positions.

### Backend features with no screen yet
7. **Build portfolio from momentum** (`POST /portfolio/{id}/build`).
8. **Multiple portfolios:** the API supports several per user; pages always use
   the most recent and there's no create/switch UI.
9. **Politician trades** (`/politicians/*`, needs `QUIVER_API_KEY`).

### Bigger features (not started)
10. **Watchlists and price alerts**, plus a per-stock performance drill-down.
11. **Options pricing:** premiums are typed by hand (no free options price
    feed) and there's no 100-share contract multiplier.
12. ~~**AI autopilot**~~ ✅ Done Oct 10: rules-based autopilot with guardrails
    and a daily-loss kill switch (see README → Autopilot). Next steps: a
    performance view comparing it to the S&P 500, and trimming positions that
    grow past the per-stock cap.
13. **Own sentiment model (optional):** train a local model so sentiment runs
    free and offline. Scaffolding exists (`news_sentiment/model/`,
    `SENTIMENT_BACKEND=local`) but nothing is trained and PyTorch isn't
    installed. Plan: save every Claude-labelled headline as training data,
    fine-tune DistilBERT on public financial datasets plus those headlines,
    measure agreement with Claude on held-out headlines, then switch via `.env`.

### Optional
14. **Real-time prices:** a paid Massive key (`MASSIVE_API_KEY`) already
    switches quotes and history over; streaming quotes are not built.
15. **Money as integer cents** instead of decimals rounded to cents, before
    adding fees or tax lots.
16. **Ship container logs to Cloud Logging** (Docker `gcplogs` driver): today they
    live only on the VM's disk and would be lost with it.
17. **Remove the old default service account's leftover access** (Artifact Registry
    reader) now that the VM runs as `mahokshahvata-vm`.

## Done Oct 22 (health check)

- Full check of the code, live site, cloud resources and every page: 160 tests pass,
  all 8 pages and 83 buttons work with no errors, 5 of 5 daily database backups
  succeeded, no user-facing server errors since the last deploy.
- Fixed: the Yahoo Finance news feed was dead (Yahoo retired the address); now uses
  its S&P 500 and Dow feeds. An idle database connection was once cut; added TCP
  keepalives and a safe one-time retry for reads. Deployed as `v5`.
- The last copy of the old Firebase key file was shredded from the VM.

## Done Oct 18 (Milestone 4)

- **Firestore → Cloud SQL** (private IP only), 39 of 39 rows copied with ids kept;
  politician trades moved from SQLite into the same database.
- **Bucket** for the market cache, sentiment history and the new **profile photos**.
- **Secret Manager** for the database passwords and the Anthropic key; no `.env` and
  no key file on the VM (the Firebase Admin key was deleted). Google sign-in checks
  tokens with the project id alone.
- **Dedicated service account** `mahokshahvata-vm` with per-resource roles.
- `v3` deployed; maintenance page during the move.
- **Google sign-in moved to the team's own Firebase project `mahokshahvata`** (`v4`);
  existing Google users land in their same accounts. The old project
  `mahokshahvata-ae5f9` belongs to another account.
- **Oct 22: the last copy of the old Firebase key file was shredded from the VM**
  (from an old Milestone 2 folder in another member's home directory); a full disk scan
  finds no key file. The folder's old code and Oct 1 SQLite file are still there, unused.
- **Open: revoke the old keys.** Google still lists two active keys for
  `firebase-adminsdk-fbsvc@mahokshahvata-ae5f9` (`8a55d36c…`, `b663c083…`). Only that
  project's owner can delete them; they reach only the old Firestore copy, not Cloud SQL.
- Still to do for the submission: screenshots, the 2 slides and the video (see MILESTONE4.md).

## Done Oct 17

- **`v2` deployed** to the VM (`sha256:b9e27321…`; roll back with
  `TAG=v1 ./deploy/docker_deploy.sh`): sign-in rate limiting, live page labels
  instead of hardcoded ones, and the autopilot from Oct 10.

## Done since the last update (Sep 24 → Oct 10)

- **Real sign-in** (was demo-only): email/password and Google, profile page,
  sessions, sign-in history.
- **Hosting:** GCP VM with firewall and budgets (Milestone 2), then Docker,
  Artifact Registry and CI (Milestone 3).
- **Firestore** replaced SQLite for accounts and portfolios.
- **Explain this stock**, SEC fundamentals, and a real 90-day price chart
  (the fabricated chart is gone).
- **Related news** no longer shows unrelated headlines.
- **Headline → ticker matching:** "Company (TICKER)" mentions, ~1,000 company
  names, cleaner name parsing (63% → 98% on a live test).
- **One account per email**, with Google sign-in joining an existing account.
- **Fix:** an old Brotli library made every Claude call fail silently;
  `brotli>=1.2` is now required.
- **Tests no longer need the internet** (the momentum test used live prices).

## Suggested order
1. ~~Sign-in rate limiting (item 1)~~ ✅
2. ~~Replace the hardcoded labels (item 6)~~ ✅
3. Portfolio build and multiple portfolios UI (items 7–8): the backend exists.
4. Watchlists and alerts (item 10), then options pricing (11). The own
   sentiment model (13) is independent.
