# Project Status

_Last updated: 2026-10-10 (after Milestone 3: containers)_

Picking this up? Read [HANDOFF.md](HANDOFF.md) first (setup, architecture,
gotchas, and a starting point for each open task).

## Where we are

**Live website, running from a Docker image.** The app runs at
http://34.122.164.209 on a Google Cloud VM (firewalled to the team's IPs), with
real sign-in and accounts stored in Cloud Firestore. It tracks the 3,000
largest US stocks (~15 min delayed), reads live news with Claude, and lets each
user paper-trade stocks and options. The same image (`app:v1` in Artifact
Registry) runs on laptops with `docker compose up` and on the VM. 117 automated
tests pass, and GitHub Actions runs them plus a Docker build on every push.

## What works today

| Area | What a user can do |
|---|---|
| **Sign-in** | Create an account with email and password, or Continue with Google. One account per email; 30-day sessions; clear messages for a taken email or a wrong password |
| **Profile** | Change name; email and password for email accounts (changing the password signs out other devices) |
| **Overview** | Account value, P/L, risk level, holdings; live Top Movers per sector (AI / Tech / Health / Energy) |
| **Market** | Search, sort and filter the 3,000 largest US stocks; stock detail with a real 90-day price chart, SEC fundamentals, related news, and **Explain this stock** (a plain-English summary by Claude, or by rules without a key) |
| **Portfolio** | Holdings at live prices; P/L against money deposited; sector breakdown; daily value chart; market-order trading by shares or dollars (fractional, $1 minimum, cash checks); trade history |
| **Sentiment** | Headlines from 4 RSS sources (or Benzinga), labelled by Claude Haiku 4.5; each headline tagged with the stocks it names (48 of 49 found on a live test, no wrong tags); Good / Bad / Wait verdicts; "Good to invest in right now" picks |
| **Options** | Buy and close calls and puts with a cost preview; open positions; options history |
| **Billing** | Add funds; cash / allocation / risk views |

Behind the scenes:
- **Hosting:** Docker image (non-root, health-checked, no secrets inside) in
  Artifact Registry; the VM runs it with Docker Compose behind Caddy. Deploy or
  roll back with `TAG=<version> ./deploy/docker_deploy.sh`. See
  [deploy/MILESTONE3.md](deploy/MILESTONE3.md).
- **Data:** accounts, portfolios, sessions and sign-in history in Firestore
  (Firebase project `mahokshahvata-ae5f9`); market cache in a Docker volume.
- **Cost controls:** $50 budget with alerts at 10/20/25/50/100% plus a $100
  budget; free-tier e2-micro; registry cleanup policy; capped container logs.
  Current spend is $0.00 after the free tier.
- **Team workflow:** both of us push to `master`; a Claude Code hook warns
  before committing or pushing when the other has pushed (local setting, not
  committed).

## What's left

### Security and accounts
1. **Sign-in rate limiting.** Passwords can be guessed without limit. Add
   per-IP and per-email throttling on `POST /auth/login`. Highest priority.
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
6. **Hardcoded labels** that never change: Overview "Signal quality: Strong"
   and the "AI Market Signal" card (Technology Bullish / Healthcare Stable /
   Energy Cautious); Portfolio "Balanced Growth"; Options "Defined risk";
   Market "Scanning in real time". Compute them (e.g. from the sentiment
   sector suggestions) or remove them.

### Backend features with no screen yet
7. **Build portfolio from momentum** (`POST /portfolio/{id}/build`).
8. **Multiple portfolios:** the API supports several per user; pages always use
   the most recent and there's no create/switch UI.
9. **Politician trades** (`/politicians/*`, needs `QUIVER_API_KEY`).

### Bigger features (not started)
10. **Watchlists and price alerts**, plus a per-stock performance drill-down.
11. **Options pricing:** premiums are typed by hand (no free options price
    feed) and there's no 100-share contract multiplier.
12. **AI autopilot:** automatic buy/sell from signals, with explicit risk
    guardrails (position limits, daily loss limit, kill switch).
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
1. Sign-in rate limiting (item 1): small, and closes the biggest risk.
2. Replace the hardcoded labels (item 6): small, and the first thing a reviewer
   notices on the Overview page.
3. Portfolio build and multiple portfolios UI (items 7–8): the backend exists.
4. Watchlists and alerts (item 10), then options pricing (11), then autopilot
   (12) once the rest is stable. The own sentiment model (13) is independent.
