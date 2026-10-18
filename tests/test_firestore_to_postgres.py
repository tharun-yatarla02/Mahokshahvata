"""The Firestore -> PostgreSQL copy keeps ids and every row, and new rows
continue after the copied ids."""
import pytest

from db.firestore_to_postgres import TABLES, copy, postgres_counts, report

FIRESTORE = {
    "users": [
        {"id": 1, "firebase_uid": "local:abc", "email": "ada@example.com", "name": "Ada", "password_hash": "scrypt$x",
         "created_at": "2026-09-25T10:00:00+00:00"},
        {"id": 7, "firebase_uid": "g-123", "email": "grace@example.com", "name": "Grace", "password_hash": None,
         "created_at": "2026-10-01T10:00:00+00:00"},
    ],
    "sessions": [{"token_hash": "h1", "user_id": 7, "created_at": "2026-10-01T10:00:00+00:00",
                  "expires_at": "2026-10-31T10:00:00+00:00"}],
    "auth_events": [{"event": "login", "user_id": 7, "method": "google", "email": "grace@example.com", "ip": "1.2.3.4",
                     "user_agent": "x", "at": "2026-10-01T10:00:00+00:00"},
                    {"event": "login_failed", "user_id": 99, "method": "email", "email": "gone@example.com", "ip": None,
                     "user_agent": None, "at": "2026-10-02T10:00:00+00:00"}],  # user 99 no longer exists
    "portfolios": [{"id": 3, "user_id": 7, "name": "Demo Portfolio", "starting_capital": 100000.0,
                    "cash_balance": 94983.0, "contributed_capital": 100000.0, "created_at": "2026-10-01T10:00:00+00:00",
                    "autopilot": {"enabled": True, "managed": ["AAPL"]}}],
    "holdings": [{"portfolio_id": 3, "position_key": "AAPL", "ticker": "AAPL", "sector": "Technology",
                  "instrument_type": "STOCK", "option_type": None, "strike": None, "expiry": None,
                  "quantity": 14.619883, "avg_buy_price": 342.0},
                 {"portfolio_id": 3, "position_key": "AAPL|CALL|350.0|2026-12-18", "ticker": "AAPL", "sector": "Options",
                  "instrument_type": "OPTION", "option_type": "CALL", "strike": 350.0, "expiry": "2026-12-18",
                  "quantity": 2, "avg_buy_price": 8.5}],
    "transactions": [{"portfolio_id": 3, "ticker": "AAPL", "instrument_type": "STOCK", "type": "BUY",
                      "quantity": 14.619883, "price": 342.0, "source": "autopilot", "timestamp": "2026-10-10T14:00:00Z"}],
    "snapshots": [{"portfolio_id": 3, "as_of": "2026-10-10", "total_value": 100000.0, "cash_balance": 94983.0,
                   "holdings_value": 5017.0, "total_pl": 0.0, "total_pl_pct": 0.0}],
    "autopilot_runs": [{"portfolio_id": 3, "at": "2026-10-10T14:00:00+00:00", "trigger": "manual", "status": "ok",
                        "value_before": 100000.0, "trades": [{"ticker": "AAPL"}], "notes": []}],
}


@pytest.fixture
def engine(make_engine):
    return make_engine()


def test_copy_keeps_every_row_and_id(engine):
    import copy as deep
    data = deep.deepcopy(FIRESTORE)
    before = {t: len(rows) for t, rows in data.items()}
    copy(engine.conn, data)
    after = postgres_counts(engine.conn)
    assert after == before
    assert "NO" not in report(before, after)

    user_id = engine.get_session_user("h1")
    assert user_id == 7                                                  # same ids, sessions still valid
    summary = engine.get_portfolio_summary(3, 7)
    assert summary["cash_balance"] == 94983.0 and len(summary["holdings"]) == 2
    assert engine.get_autopilot(3, 7)["managed"] == ["AAPL"]
    assert engine.get_trade_history(3, 7)[0]["source"] == "autopilot"
    # New rows don't collide with the copied ids.
    assert engine.create_local_user("new@example.com", "New", "scrypt$y") == 8
    assert engine.create_portfolio(8, "Next", 1000) == 4


def test_tables_list_covers_every_engine_table(engine):
    tables = {r["table_name"] for r in engine._rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    assert tables - {"schema_migrations", "politician_trades"} == set(TABLES)
