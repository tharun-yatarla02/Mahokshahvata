"""
firestore_to_postgres.py

One-off move of the app's data from Firestore (Milestones 2-3) into
PostgreSQL (Milestone 4), keeping every id, then prints the row counts of
both side by side. Runs on the VM, the only machine that reaches Cloud SQL's
private IP, in a throwaway container from the app image:

    python -m db.firestore_to_postgres --count-only   # Firestore counts, writes nothing
    python -m db.firestore_to_postgres                # copy, then compare counts

Firestore is read with the old Firebase Admin key (FIRESTORE_KEY_FILE), used for
nothing else; the database password comes from Secret Manager as the VM's own
service account (SECRETS_PROJECT). The target tables must be empty, and the
copy is one transaction: it lands completely or not at all.
"""

import argparse
import json
import os
import sys

from psycopg.types.json import Jsonb

from cloud.secrets import load_secrets
from db.connection import connect
from db.migrate import migrate

TABLES = ["users", "sessions", "auth_events", "portfolios", "holdings", "transactions", "snapshots", "autopilot_runs"]
SUBCOLLECTIONS = ["holdings", "transactions", "snapshots", "autopilot_runs"]  # under portfolios/{id}


def read_firestore(db):
    """{table: [rows]} straight from Firestore; subcollection rows get their portfolio_id."""
    data = {name: [] for name in TABLES}
    for name in ("users", "auth_events"):
        data[name] = [d.to_dict() for d in db.collection(name).stream()]
    data["sessions"] = [{**d.to_dict(), "token_hash": d.id} for d in db.collection("sessions").stream()]
    for portfolio in db.collection("portfolios").stream():
        data["portfolios"].append(portfolio.to_dict())
        pid = int(portfolio.id)
        for sub in SUBCOLLECTIONS:
            for d in portfolio.reference.collection(sub).stream():
                row = {**d.to_dict(), "portfolio_id": pid}
                if sub == "holdings":
                    row.setdefault("position_key", d.id)
                if sub == "snapshots":
                    row.setdefault("as_of", d.id)
                data[sub].append(row)
    return data


def _insert(conn, table, columns, rows):
    if rows:
        placeholders = ", ".join(["%s"] * len(columns))
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                            [tuple(row.get(c) for c in columns) for row in rows])


def copy(conn, data):
    user_ids = {u["id"] for u in data["users"]}
    portfolio_ids = {p["id"] for p in data["portfolios"]}
    for event in data["auth_events"]:  # history can mention an account that no longer exists
        if event.get("user_id") not in user_ids:
            event["user_id"] = None
    for portfolio in data["portfolios"]:
        portfolio["autopilot"] = Jsonb(portfolio.get("autopilot") or {})
    for run in data["autopilot_runs"]:
        run["trades"], run["notes"] = Jsonb(run.get("trades") or []), Jsonb(run.get("notes") or [])
    orphans = [r for t in SUBCOLLECTIONS for r in data[t] if r["portfolio_id"] not in portfolio_ids]
    if orphans:
        raise SystemExit(f"{len(orphans)} subcollection rows point at missing portfolios; not copying")

    with conn.transaction():
        _insert(conn, "users", ["id", "firebase_uid", "email", "name", "password_hash", "created_at"], data["users"])
        _insert(conn, "sessions", ["token_hash", "user_id", "created_at", "expires_at"],
                [s for s in data["sessions"] if s.get("user_id") in user_ids])
        _insert(conn, "auth_events", ["event", "user_id", "method", "email", "ip", "user_agent", "at"], data["auth_events"])
        _insert(conn, "portfolios", ["id", "user_id", "name", "starting_capital", "cash_balance", "contributed_capital",
                                     "autopilot", "created_at"], data["portfolios"])
        _insert(conn, "holdings", ["portfolio_id", "position_key", "ticker", "sector", "instrument_type", "option_type",
                                   "strike", "expiry", "quantity", "avg_buy_price"], data["holdings"])
        _insert(conn, "transactions", ["portfolio_id", "ticker", "instrument_type", "option_type", "strike", "expiry",
                                       "type", "quantity", "price", "source", "timestamp"], data["transactions"])
        _insert(conn, "snapshots", ["portfolio_id", "as_of", "total_value", "cash_balance", "holdings_value",
                                    "total_pl", "total_pl_pct"], data["snapshots"])
        _insert(conn, "autopilot_runs", ["portfolio_id", "at", "trigger", "status", "value_before", "trades", "notes"],
                data["autopilot_runs"])
        # New rows continue after the copied ids instead of colliding with them.
        for table in ("users", "portfolios"):
            conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                         f"GREATEST((SELECT max(id) FROM {table}), 1))")


def postgres_counts(conn):
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in TABLES}


def report(before, after):
    lines = [f"{'table':<16}{'Firestore':>11}{'PostgreSQL':>12}  match", "-" * 46]
    for t in TABLES:
        lines.append(f"{t:<16}{before[t]:>11}{after[t]:>12}  {'yes' if before[t] == after[t] else 'NO'}")
    lines.append("-" * 46)
    lines.append(f"{'total':<16}{sum(before.values()):>11}{sum(after.values()):>12}  "
                 f"{'yes' if before == after else 'NO'}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count-only", action="store_true", help="print Firestore's row counts and stop")
    parser.add_argument("--project", default="mahokshahvata-ae5f9")  # where the Milestone 3 data was
    args = parser.parse_args()

    from google.cloud import firestore
    from google.oauth2 import service_account
    key_file = os.environ.get("FIRESTORE_KEY_FILE")
    creds = service_account.Credentials.from_service_account_file(key_file) if key_file else None
    data = read_firestore(firestore.Client(project=args.project, credentials=creds))
    before = {t: len(rows) for t, rows in data.items()}
    if args.count_only:
        print(json.dumps(before, indent=2))
        return

    load_secrets()  # DB_PASSWORD
    with connect() as conn:
        migrate(conn)
        if any(postgres_counts(conn).values()):
            sys.exit("PostgreSQL already has rows; refusing to copy over them.")
        copy(conn, data)
        after = postgres_counts(conn)
    print(report(before, after))
    sys.exit(0 if before == after else 1)


if __name__ == "__main__":
    main()
