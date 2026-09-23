"""
google_auth.py

Verifies "Sign in with Google" ID tokens on the backend, and issues our
own short-lived session token for authenticated API calls afterward.

Setup:
    pip install google-auth pyjwt

    1. Go to console.cloud.google.com -> APIs & Services -> Credentials
    2. Create an OAuth 2.0 Client ID (Application type: "Web application")
    3. Under "Authorized JavaScript origins," add your frontend's URL
       (e.g. http://localhost:5500 for local dev, or your deployed
       dashboard's real URL once Part 7 is done)
    4. Copy the Client ID it gives you:
       export GOOGLE_CLIENT_ID="your-id.apps.googleusercontent.com"
    5. Also set a secret for OUR OWN session tokens (not Google's) —
       any random string, kept private:
       export SESSION_SECRET="something-long-and-random"

    No Google client SECRET is needed for this flow — only the Client ID.
    That's specific to this exact flow (verifying an ID token that Google
    Identity Services issued directly to the frontend); a server-side
    OAuth flow would need the secret too, but this one doesn't.

The flow, end to end:
    1. Frontend renders Google's official Sign-In button (Google Identity
       Services JS library — a <script> tag, not something we write)
    2. User signs in with their Google account
    3. Google hands the FRONTEND an ID token (a signed JWT proving who
       the user is) — our backend never sees the user's Google password
    4. Frontend POSTs that ID token to our /auth/google endpoint
    5. verify_google_id_token() below checks Google's signature on it AND
       checks the token was issued for OUR app specifically (the
       "audience" check) — this stops someone reusing a token meant for
       a different app
    6. We look up or create a local user record keyed by Google's stable
       user id (the "sub" claim)
    7. We issue our OWN session token (issue_session_token) and send that
       back to the frontend
    8. Frontend attaches it to every future API call:
       Authorization: Bearer <session token>
    9. verify_session_token() checks that on each request
"""

import os
import time

import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me-before-deploying")
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # session tokens last 7 days


def verify_google_id_token(token):
    """Verifies a Google ID token came from Google and was issued for this
    app. Returns the user's info from it, or raises ValueError if it's
    invalid, expired, or meant for a different app."""
    if not GOOGLE_CLIENT_ID:
        raise RuntimeError("GOOGLE_CLIENT_ID is not set in the environment.")
    try:
        info = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    except ValueError as e:
        raise ValueError(f"Invalid Google ID token: {e}")

    return {
        "google_sub": info["sub"],  # Google's stable, unique id for this user — use this as the key, not email
        "email": info.get("email"),
        "name": info.get("name"),
        "picture": info.get("picture"),
    }


def issue_session_token(user_id):
    """Issues our own short-lived session token after Google verification succeeds."""
    payload = {
        "user_id": user_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm="HS256")


def verify_session_token(token):
    """Verifies OUR session token (not Google's). Returns the user_id, or
    raises ValueError if it's invalid or expired."""
    try:
        payload = jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise ValueError(f"Invalid or expired session token: {e}")
    return payload["user_id"]
