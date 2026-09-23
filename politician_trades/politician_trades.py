"""
politician_trades.py

Fetches U.S. congressional stock trading disclosures (required under the
STOCK Act) via the Quiver Quantitative API, caches them locally, and
exposes two things your dashboard needs:
  - a list of politicians who have disclosed trades
  - for a selected politician, every trade sorted newest to oldest

Setup:
    pip install requests
    Get a free-tier API key at https://www.quiverquant.com/
    export QUIVER_API_KEY="your-key-here"

Run:
    python politician_trades.py                 # refresh cache, print a sample
    python politician_trades.py "Nancy Pelosi"   # print one politician's trades

Note on the API endpoint:
    Quiver's exact endpoint paths have changed before and may change again —
    verify against https://api.quiverquant.com/docs before relying on this
    in production. The bulk endpoint used below (congresstrading) is the
    one documented for pulling all recent disclosures at once; adjust
    BULK_ENDPOINT if their docs show something different when you check.
"""

import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

QUIVER_API_KEY = os.environ.get("QUIVER_API_KEY")
BULK_ENDPOINT = "https://api.quiverquant.com/beta/bulk/congresstrading"
DB_PATH = Path(__file__).parent / "politician_trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    politician TEXT NOT NULL,
    chamber TEXT,
    party TEXT,
    ticker TEXT,
    transaction_type TEXT,
    transaction_date TEXT,
    disclosure_date TEXT,
    amount_range TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(politician, ticker, transaction_date, transaction_type, amount_range)
);
CREATE INDEX IF NOT EXISTS idx_politician ON trades(politician);
CREATE INDEX IF NOT EXISTS idx_transaction_date ON trades(transaction_date);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def fetch_from_quiver():
    """Pulls the latest bulk congressional trading data from Quiver Quantitative."""
    if not QUIVER_API_KEY:
        raise RuntimeError("QUIVER_API_KEY is not set in the environment.")

    headers = {"Authorization": f"Bearer {QUIVER_API_KEY}"}
    response = requests.get(BULK_ENDPOINT, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def refresh_cache():
    """Fetches fresh data from Quiver and stores it locally, so the dashboard
    can query instantly without hitting the API on every page load."""
    raw_trades = fetch_from_quiver()
    conn = get_connection()
    fetched_at = datetime.now(timezone.utc).isoformat()

    inserted = 0
    for t in raw_trades:
        # Field names below follow Quiver's documented response shape —
        # double check these keys against their docs if this errors, APIs
        # do rename fields between versions.
        # INSERT OR IGNORE: with the UNIQUE constraint above, re-running this
        # on a schedule against overlapping data won't create duplicate rows
        # — only genuinely new trades get inserted.
        cur = conn.execute(
            """INSERT OR IGNORE INTO trades (politician, chamber, party, ticker, transaction_type,
                                    transaction_date, disclosure_date, amount_range, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.get("Representative") or t.get("Senator") or t.get("Name"),
                t.get("Chamber") or ("Senate" if "Senator" in t else "House"),
                t.get("Party"),
                t.get("Ticker"),
                t.get("Transaction"),
                t.get("TransactionDate"),
                t.get("ReportDate") or t.get("DisclosureDate"),
                t.get("Range") or t.get("Amount"),
                fetched_at,
            ),
        )
        if cur.rowcount:  # actually inserted, not skipped as a duplicate
            inserted += 1

    conn.commit()
    print(f"Refreshed cache: {inserted} new trade record(s) stored ({len(raw_trades)} fetched, rest already known).")
    return inserted


def list_politicians():
    """Returns every politician with at least one disclosed trade, alphabetical."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT politician, party, chamber FROM trades WHERE politician IS NOT NULL ORDER BY politician"
    ).fetchall()
    return [dict(r) for r in rows]


def get_trades_for_politician(name):
    """Every trade for one politician, newest transaction date first."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT ticker, transaction_type, transaction_date, disclosure_date, amount_range
           FROM trades
           WHERE politician = ?
           ORDER BY transaction_date DESC""",
        (name,),
    ).fetchall()
    return [dict(r) for r in rows]


def export_politician_csv(name, path=None):
    trades = get_trades_for_politician(name)
    path = path or f"{name.replace(' ', '_')}_trades.csv"
    if not trades:
        print(f"No trades found for {name}.")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        writer.writeheader()
        writer.writerows(trades)
    print(f"Exported {len(trades)} trades for {name} to {path}")


if __name__ == "__main__":
    try:
        refresh_cache()
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) > 1:
        name = sys.argv[1]
        trades = get_trades_for_politician(name)
        print(f"\n{len(trades)} trade(s) for {name} (newest first):\n")
        for t in trades:
            print(f"  {t['transaction_date']}  {t['transaction_type']:6}  {t['ticker']:6}  {t['amount_range']}")
    else:
        politicians = list_politicians()
        print(f"\n{len(politicians)} politician(s) with disclosed trades:")
        for p in politicians[:15]:
            print(f"  {p['politician']} ({p.get('party', '?')}, {p.get('chamber', '?')})")
