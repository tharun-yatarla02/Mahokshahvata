"""
firebase_auth.py

Verifies Firebase ID tokens on the backend. Replaces the earlier raw-Google-
Cloud-OAuth approach (google_auth.py) — Firebase manages the sign-in flow
and issues its own tokens, so there's no separate "our own session token"
step needed: a valid Firebase ID token IS a valid session, on every request.

Why this instead of raw Google Cloud OAuth:
    Google's OAuth "brand verification" requirement only applies once you
    publish an app to Production for the general public. Firebase sidesteps
    this for typical small/student projects: enabling Google as a sign-in
    provider inside Firebase auto-configures everything without the manual
    verification wall raw Google Cloud OAuth can hit. No domain, no company
    registration needed.

Setup:
    pip install firebase-admin

    1. Go to console.firebase.google.com -> Create a project (free, no
       domain or company needed)
    2. Build -> Authentication -> Get started -> enable "Google" as a
       sign-in provider (one click)
    3. Project settings (gear icon) -> Service accounts -> "Generate new
       private key" -> downloads a JSON file. Keep this private — it's a
       credential, not something to commit to git.
    4. Point the backend at it:
       export GOOGLE_APPLICATION_CREDENTIALS="/path/to/serviceAccountKey.json"
    5. In your frontend, add the Firebase JS SDK and use
       signInWithPopup(auth, new GoogleAuthProvider()) — see auth/README.md
       for the full snippet.

The flow, end to end:
    1. Frontend uses the Firebase SDK to show Google's sign-in popup
    2. User signs in — Firebase handles the Google OAuth exchange internally
    3. Firebase SDK gives the frontend an ID token (a JWT, auto-refreshed
       by the SDK as it nears expiry — no manual refresh logic needed)
    4. Frontend attaches it to every API call: Authorization: Bearer <token>
    5. verify_firebase_token() below checks it's genuinely signed by
       Firebase and not expired, and returns the user's info
    6. There is no separate "our own session token" — the Firebase token
       itself is checked on every request
"""

import os
import threading

import firebase_admin
from firebase_admin import auth as firebase_auth_sdk
from firebase_admin import credentials

_app = None
_app_lock = threading.Lock()
DEMO_TOKEN = "demo-token"


def _get_app():
    """Lazily initializes the Firebase Admin SDK on first use, using the
    service account JSON pointed to by GOOGLE_APPLICATION_CREDENTIALS."""
    global _app
    with _app_lock:  # two first requests at once would both try to initialize
        if _app is None:
            _app = firebase_admin.initialize_app(credentials.ApplicationDefault())
    return _app


def verify_firebase_token(token):
    """Verifies a Firebase ID token. Returns the user's info, or raises
    ValueError if it's invalid, expired, or malformed.

    When USE_DEMO_AUTH=true, the single token "demo-token" signs in as a local
    demo user, so the app can be exercised without a Firebase project or
    service account configured. Only that exact token is accepted: accepting
    any string as the user id would let a caller sign in as anyone just by
    sending their id, if demo mode were ever left on in a real deployment.
    """
    if os.getenv("USE_DEMO_AUTH", "").lower() in {"1", "true", "yes", "on"}:
        if token is None or not str(token).strip():
            raise ValueError("Missing demo auth token")
        if str(token).strip() != DEMO_TOKEN:
            raise ValueError(f'Demo mode only accepts the token "{DEMO_TOKEN}"')
        return {
            "firebase_uid": DEMO_TOKEN,
            "email": "demo@example.com",
            "name": "Local Demo User",
            "picture": None,
        }

    try:
        _get_app()
    except Exception as e:  # e.g. GOOGLE_APPLICATION_CREDENTIALS not set
        raise ValueError(f"Firebase is not configured on the server: {e}")
    try:
        decoded = firebase_auth_sdk.verify_id_token(token)
    except Exception as e:  # firebase_admin raises several distinct error
        # types (ExpiredIdTokenError, InvalidIdTokenError, etc.) — catching
        # broadly here and re-raising as one clear ValueError keeps the
        # caller's error handling simple.
        raise ValueError(f"Invalid Firebase ID token: {e}")

    return {
        "firebase_uid": decoded["uid"],  # Firebase's stable, unique id for this user — use this as the key
        "email": decoded.get("email"),
        "name": decoded.get("name"),
        "picture": decoded.get("picture"),
    }
