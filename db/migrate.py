"""
migrate.py

Applies the SQL files in db/migrations/ in name order, each once, in its own
transaction, and records them in schema_migrations. Runs at app startup (so
a new image brings its own schema) and by hand:

    python -m db.migrate            # apply pending migrations
    python -m db.migrate --status   # list applied and pending ones

An advisory lock keeps two processes from migrating at the same time.
"""

import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_ID = 727274  # any constant; shared by every process migrating this database


def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                        version    TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def status(conn):
    """(applied, pending) migration file names."""
    _ensure_table(conn)
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in files if f in applied], [f for f in files if f not in applied]


def migrate(conn):
    """Applies pending migrations; returns the names applied."""
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
    try:
        _, pending = status(conn)
        for name in pending:
            with conn.transaction():
                conn.execute((MIGRATIONS_DIR / name).read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (name,))
        return pending
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))


if __name__ == "__main__":
    from db.connection import connect

    with connect() as conn:
        if "--status" in sys.argv:
            applied, pending = status(conn)
            print("applied:", ", ".join(applied) or "none")
            print("pending:", ", ".join(pending) or "none")
        else:
            done = migrate(conn)
            print("applied:", ", ".join(done) if done else "nothing to do")
