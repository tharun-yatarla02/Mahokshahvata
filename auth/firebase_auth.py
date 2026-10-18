"""
firebase_auth.py

Verifies Firebase ID tokens on the backend. The sign-in page gets one from
Firebase's Google popup and posts it to POST /auth/google, which checks it
here and then starts one of our own sessions (see auth/README.md). A raw
Firebase ID token is also still accepted as a Bearer token on any endpoint.

Why Firebase instead of raw Google Cloud OAuth:
    Google's OAuth "brand verification" requirement only applies once you
    publish an app to Production for the general public. Firebase sidesteps
    this for typical small/student projects: enabling Google as a sign-in
    provider inside Firebase auto-configures everything without the manual
    verification wall raw Google Cloud OAuth can hit. No domain, no company
    registration needed.

Setup: checking an ID token needs only the Firebase project id
(FIREBASE_PROJECT_ID, default mahokshahvata-ae5f9): the SDK verifies the
token's signature against Google's public keys. No service-account key file
is needed, and none is used on the VM. Full steps in auth/README.md.
"""

import os
import threading

import firebase_admin
from firebase_admin import auth as firebase_auth_sdk
from firebase_admin import credentials

_app = None
_app_lock = threading.Lock()
DEMO_TOKEN = "demo-token"


class _NoCredentials(credentials.Base):
    """verify_id_token only checks signatures with Google's public keys, so the
    SDK needs no credentials; this keeps it from looking for a key file."""

    def get_credential(self):
        return None


def _get_app():
    """Lazily initializes the Firebase Admin SDK on first use, for token checks only."""
    global _app
    with _app_lock:  # two first requests at once would both try to initialize
        if _app is None:
            project = os.environ.get("FIREBASE_PROJECT_ID", "mahokshahvata-ae5f9")
            _app = firebase_admin.initialize_app(_NoCredentials(), {"projectId": project})
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
            "email_verified": False,
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
        "email_verified": bool(decoded.get("email_verified")),  # Google checked the user owns this email
    }
