"""
find_duplicate_emails.py

Read-only: lists emails used by more than one account, with each account's
sign-in method and portfolios, so duplicates made before the
one-account-per-email rule can be merged by hand. Writes nothing. Run it on
the VM (the only machine that reaches the database), from the app image:

    sudo docker compose -p mahokshahvata exec app python -m db.find_duplicate_emails
"""

from cloud.secrets import load_secrets
from db.connection import connect


def main():
    load_secrets()
    with connect() as conn:
        rows = conn.execute("""
            SELECT u.email, u.id, u.name, u.created_at,
                   CASE WHEN u.password_hash IS NOT NULL THEN 'password'
                        WHEN u.firebase_uid = 'demo-token' THEN 'demo' ELSE 'google' END AS kind,
                   COALESCE(string_agg(p.name || ' ($' || to_char(p.cash_balance, 'FM999,999,990.00') || ' cash)',
                                       '; ' ORDER BY p.id), '(no portfolios)') AS portfolios
            FROM users u LEFT JOIN portfolios p ON p.user_id = u.id
            WHERE lower(u.email) IN (SELECT lower(email) FROM users WHERE email <> ''
                                     GROUP BY lower(email) HAVING count(*) > 1)
            GROUP BY u.id ORDER BY lower(u.email), u.id""").fetchall()
        total = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
    emails = {r["email"].lower() for r in rows}
    print(f"{total} accounts, {len(emails)} emails used more than once")
    current = None
    for r in rows:
        if r["email"].lower() != current:
            current = r["email"].lower()
            print(f"\n{current}")
        print(f"  user {r['id']:>4}  {r['kind']:8}  {r['name']!r}  created {r['created_at']}")
        print(f"              {r['portfolios']}")


if __name__ == "__main__":
    main()
