"""
One-off: copies paper_trading.db (the old SQLite store) into Firestore,
keeping every id, so existing sign-ins, portfolio URLs and sessions keep
working. Safe to re-run: every document id is fixed, so a second run
overwrites instead of duplicating. The .db file is left untouched as a backup.

    GOOGLE_APPLICATION_CREDENTIALS=~/.config/mahokshahvata/firebase-admin.json \\
        python deploy/migrate_sqlite_to_firestore.py paper_trading/paper_trading.db
"""

import sqlite3
import sys

from google.cloud import firestore


def migrate(db_path, db):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = lambda table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    batch, pending = db.batch(), 0

    def put(ref, data):
        nonlocal batch, pending
        batch.set(ref, data)
        pending += 1
        if pending == 400:  # Firestore caps a batch at 500 writes
            batch.commit()
            batch, pending = db.batch(), 0

    users = rows("users")
    for u in users:
        put(db.collection("users").document(str(u["id"])), {
            "id": u["id"], "firebase_uid": u["firebase_uid"], "email": u["email"], "name": u["name"],
            "password_hash": u.get("password_hash"), "created_at": u["created_at"],
        })
    for s in rows("sessions"):
        put(db.collection("sessions").document(s["token_hash"]),
            {"user_id": s["user_id"], "created_at": s["created_at"], "expires_at": s["expires_at"]})

    portfolios = rows("portfolios")
    for p in portfolios:
        put(db.collection("portfolios").document(str(p["id"])), p)
    for h in rows("holdings"):
        h.pop("id")
        put(db.collection("portfolios").document(str(h["portfolio_id"])).collection("holdings").document(h["position_key"]), h)
    for t in rows("transactions"):
        doc_id = f"sqlite-{t.pop('id')}"
        put(db.collection("portfolios").document(str(t["portfolio_id"])).collection("transactions").document(doc_id), t)
    for snap in rows("daily_snapshots"):
        snap.pop("id")
        put(db.collection("portfolios").document(str(snap["portfolio_id"])).collection("snapshots").document(snap["as_of"]), snap)

    # New ids continue after the migrated ones.
    put(db.collection("counters").document("users"), {"last": max((u["id"] for u in users), default=0)})
    put(db.collection("counters").document("portfolios"), {"last": max((p["id"] for p in portfolios), default=0)})
    batch.commit()
    return {table: len(rows(table)) for table in ["users", "sessions", "portfolios", "holdings", "transactions", "daily_snapshots"]}


if __name__ == "__main__":
    print(migrate(sys.argv[1], firestore.Client()))
