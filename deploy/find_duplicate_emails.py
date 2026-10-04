"""
find_duplicate_emails.py

Read-only: lists emails used by more than one account in Firestore, with
each account's sign-in method and portfolios, so duplicates made before the
one-account-per-email rule can be merged by hand. Writes nothing.

    GOOGLE_APPLICATION_CREDENTIALS=~/.config/mahokshahvata/firebase-admin.json \
        python deploy/find_duplicate_emails.py
"""

import os
from collections import defaultdict

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter


def main():
    db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "mahokshahvata-ae5f9"))
    users = [d.to_dict() for d in db.collection("users").stream()]
    by_email = defaultdict(list)
    for user in users:
        by_email[(user.get("email") or "").strip().lower()].append(user)
    duplicates = {email: group for email, group in by_email.items() if email and len(group) > 1}

    print(f"{len(users)} accounts, {len(by_email)} distinct emails, {len(duplicates)} used more than once")
    for email, group in sorted(duplicates.items()):
        print(f"\n{email}")
        for user in sorted(group, key=lambda u: u["id"]):
            kind = "password" if user.get("password_hash") else "demo" if user.get("firebase_uid") == "demo-token" else "google"
            portfolios = [
                f"{p.get('name')} (${p.get('cash_balance', 0):,.2f} cash)"
                for p in (s.to_dict() for s in db.collection("portfolios")
                          .where(filter=FieldFilter("user_id", "==", user["id"])).stream())
            ]
            print(f"  user {user['id']:>4}  {kind:8}  {user.get('name')!r}  created {user.get('created_at')}")
            for line in portfolios or ["(no portfolios)"]:
                print(f"              {line}")

    odd = sorted(u.get("email") for u in users if u.get("email") and u["email"] != u["email"].strip().lower())
    if odd:
        print("\nEmails stored with capitals or spaces (matched case-insensitively above):", ", ".join(odd))


if __name__ == "__main__":
    main()
