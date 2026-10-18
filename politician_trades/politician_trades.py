"""
politician_trades.py

Fetches U.S. congressional stock trading disclosures (required under the
STOCK Act) via the Quiver Quantitative API, stores them in PostgreSQL, and
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
import sys

import requests

from db.connection import connect

BULK_ENDPOINT = "https://api.quiverquant.com/beta/bulk/congresstrading"
# Stored in PostgreSQL (table politician_trades, created by db/migrations/001_initial.sql).


def fetch_from_quiver():
    """Pulls the latest bulk congressional trading data from Quiver Quantitative."""
    api_key = os.environ.get("QUIVER_API_KEY")  # read per call: Secret Manager fills it in at startup
    if not api_key:
        raise RuntimeError("QUIVER_API_KEY is not set in the environment.")

    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(BULK_ENDPOINT, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def refresh_cache():
    """Fetches fresh data from Quiver and stores it in the database, so the
    dashboard can query instantly without hitting the API on every page load."""
    raw_trades = fetch_from_quiver()
    with connect() as conn:
        return _store_trades(conn, raw_trades)


def _store_trades(conn, raw_trades):
    inserted = 0
    with conn.transaction():
        for t in raw_trades:
            name = t.get("Representative") or t.get("Senator") or t.get("Name")
            if not name:  # unusable without a name (and the column is NOT NULL)
                continue
            # Field names below follow Quiver's documented response shape —
            # double check these keys against their docs if this errors, APIs
            # do rename fields between versions.
            # ON CONFLICT DO NOTHING: the unique index on (politician, ticker,
            # dates, type, amount) means re-running this against overlapping
            # data only inserts genuinely new trades.
            cur = conn.execute(
                """INSERT INTO politician_trades (politician, chamber, party, ticker, transaction_type,
                                                  transaction_date, disclosure_date, amount_range)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (
                    name,
                    t.get("Chamber") or ("Senate" if "Senator" in t else "House"),
                    t.get("Party"),
                    t.get("Ticker"),
                    t.get("Transaction"),
                    t.get("TransactionDate"),
                    t.get("ReportDate") or t.get("DisclosureDate"),
                    t.get("Range") or t.get("Amount"),
                ),
            )
            inserted += cur.rowcount  # 0 when skipped as a duplicate
    print(f"Refreshed cache: {inserted} new trade record(s) stored ({len(raw_trades)} fetched, rest already known).")
    return inserted


def list_politicians():
    """Returns every politician with at least one disclosed trade, alphabetical."""
    with connect() as conn:
        return conn.execute(
            "SELECT DISTINCT politician, party, chamber FROM politician_trades ORDER BY politician"
        ).fetchall()


def get_trades_for_politician(name):
    """Every trade for one politician, newest transaction date first."""
    with connect() as conn:
        return conn.execute(
            """SELECT ticker, transaction_type, transaction_date, disclosure_date, amount_range
               FROM politician_trades
               WHERE politician = %s
               ORDER BY transaction_date DESC""",
            (name,),
        ).fetchall()


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
