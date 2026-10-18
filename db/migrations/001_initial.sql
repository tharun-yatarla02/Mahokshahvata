-- 001_initial.sql: the app's tables in Cloud SQL for PostgreSQL.
-- Replaces the Firestore collections used through Milestone 3 (same fields,
-- same integer ids). Applied once by db/migrate.py, which records it in
-- schema_migrations; never edit an applied migration, add 002_*.sql instead.

CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    firebase_uid  TEXT NOT NULL UNIQUE,      -- Google uid, "local:<random>" for email accounts, or "demo-token"
    email         TEXT,                      -- stored lowercased; one account per email (enforced by the app)
    name          TEXT,
    password_hash TEXT,                      -- scrypt, email accounts only; NULL for Google accounts
    photo_path    TEXT,                      -- object name in the storage bucket, or NULL
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX users_email_idx ON users (email);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,             -- SHA-256 of the browser's token; the token itself is never stored
    user_id    BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE auth_events (
    id         BIGSERIAL PRIMARY KEY,
    event      TEXT NOT NULL,                -- register, login, login_failed, login_throttled, logout
    user_id    BIGINT REFERENCES users (id) ON DELETE SET NULL,
    method     TEXT,                         -- email, google, demo
    email      TEXT,
    ip         TEXT,
    user_agent TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX auth_events_at_idx ON auth_events (at);

CREATE TABLE portfolios (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    starting_capital    NUMERIC(16, 2) NOT NULL,
    cash_balance        NUMERIC(16, 2) NOT NULL CHECK (cash_balance >= 0),
    contributed_capital NUMERIC(16, 2) NOT NULL,
    autopilot           JSONB NOT NULL DEFAULT '{}',   -- enabled, settings, managed tickers, last run
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX portfolios_user_idx ON portfolios (user_id);

CREATE TABLE holdings (
    portfolio_id    BIGINT NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    position_key    TEXT NOT NULL,           -- ticker for stocks; "TICKER|CALL|strike|expiry" for options
    ticker          TEXT NOT NULL,
    sector          TEXT,
    instrument_type TEXT NOT NULL CHECK (instrument_type IN ('STOCK', 'OPTION')),
    option_type     TEXT CHECK (option_type IN ('CALL', 'PUT')),
    strike          NUMERIC(16, 2),
    expiry          DATE,
    quantity        NUMERIC(20, 6) NOT NULL CHECK (quantity > 0),
    avg_buy_price   DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (portfolio_id, position_key)
);

CREATE TABLE transactions (
    id              BIGSERIAL PRIMARY KEY,
    portfolio_id    BIGINT NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL,
    instrument_type TEXT NOT NULL CHECK (instrument_type IN ('STOCK', 'OPTION')),
    option_type     TEXT CHECK (option_type IN ('CALL', 'PUT')),
    strike          NUMERIC(16, 2),
    expiry          DATE,
    type            TEXT NOT NULL CHECK (type IN ('BUY', 'SELL')),
    quantity        NUMERIC(20, 6) NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    source          TEXT,                    -- "autopilot" for its trades; NULL for the user's own
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX transactions_portfolio_idx ON transactions (portfolio_id, timestamp);

CREATE TABLE snapshots (
    portfolio_id   BIGINT NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    as_of          TEXT NOT NULL,            -- New York trading day (YYYY-MM-DD); one row per day
    total_value    NUMERIC(16, 2) NOT NULL,
    cash_balance   NUMERIC(16, 2) NOT NULL,
    holdings_value NUMERIC(16, 2) NOT NULL,
    total_pl       NUMERIC(16, 2) NOT NULL,
    total_pl_pct   DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (portfolio_id, as_of)
);

CREATE TABLE autopilot_runs (
    id           BIGSERIAL PRIMARY KEY,
    portfolio_id BIGINT NOT NULL REFERENCES portfolios (id) ON DELETE CASCADE,
    at           TIMESTAMPTZ NOT NULL,
    trigger      TEXT NOT NULL,              -- manual, scheduled
    status       TEXT NOT NULL,              -- ok, idle, paused, closed
    value_before NUMERIC(16, 2),
    trades       JSONB NOT NULL DEFAULT '[]',
    notes        JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX autopilot_runs_portfolio_idx ON autopilot_runs (portfolio_id, at DESC);

-- Congressional trade disclosures from Quiver Quantitative (was a SQLite file on the VM).
CREATE TABLE politician_trades (
    id               BIGSERIAL PRIMARY KEY,
    politician       TEXT NOT NULL,
    chamber          TEXT,
    party            TEXT,
    ticker           TEXT,
    transaction_type TEXT,
    transaction_date TEXT,
    disclosure_date  TEXT,
    amount_range     TEXT,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX politician_trades_unique_idx
    ON politician_trades (politician, COALESCE(ticker, ''), COALESCE(transaction_date, ''),
                          COALESCE(transaction_type, ''), COALESCE(amount_range, ''));
CREATE INDEX politician_trades_date_idx ON politician_trades (transaction_date);
