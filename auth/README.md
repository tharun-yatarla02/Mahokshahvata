# Auth — Google Sign-In via Firebase

**Goal:** verify who a user is via their Google account, and make sure each user only ever sees their own portfolios — using Firebase instead of raw Google Cloud OAuth, to avoid Google's brand verification requirement.

**Files in this folder:**
- `firebase_auth.py` — verifies a Firebase ID token (plus the local demo token)
- `passwords.py` — email/password hashing and session tokens

The Firebase project is **`mahokshahvata-ae5f9`** (free Spark plan). Its Firestore database also holds the app's data; see the top of `paper_trading/paper_trading_engine.py`.

---

## Why Firebase instead of raw Google Cloud OAuth
Google's OAuth "brand verification" only applies once an app is published to Production for the general public. Firebase sidesteps this for a typical student/small project: enabling Google as a sign-in provider inside Firebase auto-configures everything without hitting that manual verification wall — no domain, no company registration needed.

## How the flow works
1. `frontend/login.html` loads the **Firebase JS SDK** and shows Google's sign-in popup ("Continue with Google")
2. Firebase handles the Google OAuth exchange and hands the page an ID token (a JWT that expires after an hour)
3. The page posts it to **`POST /auth/google`**; the server verifies it with `verify_firebase_token()`, creates the user on their first sign-in, records a `login` row in `auth_events`, and returns one of our own session tokens (30 days), exactly like `/auth/login` does
4. From then on the browser sends `Authorization: Bearer <session token>`; Firebase isn't involved again until the next sign-in
5. Logout deletes the session and records a `logout` row

Swapping the short-lived Firebase token for our session means the page needs no token-refresh logic, and every sign-in and sign-out passes through the server, so it can be recorded.

## Setup (already done for `mahokshahvata-ae5f9`; repeat only for a new project)
1. **console.firebase.google.com → Create a project** (skip Google Analytics)
2. **Build → Authentication → Get started → Sign-in method → Google → Enable**, pick a support email, **Save**
3. **Authentication → Settings → Authorized domains**: add every address the site is served from (the VM's IP `34.122.164.209` is there; `localhost` is by default)
4. **Project settings → Your apps → Web (`</>`)**: register an app and copy `apiKey`, `authDomain`, `projectId`, `appId` into `FIREBASE_CONFIG` in `frontend/login.html`. These are public by design.
5. **Firestore Database → Create database** (Native mode), and publish rules that deny all browser access — only the server reads and writes, via the Admin key, which bypasses rules:
   ```
   rules_version = '2';
   service cloud.firestore {
     match /databases/{database}/documents {
       match /{document=**} { allow read, write: if false; }
     }
   }
   ```
6. **Project settings → Service accounts → Generate new private key**. Keep it outside the repo at `~/.config/mahokshahvata/firebase-admin.json` (never commit it) and point the server at it:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=~/.config/mahokshahvata/firebase-admin.json
   ```
   `deploy/gcp.sh` copies it to the VM on every deploy.

## Checks
- No `Authorization` header → 401; an invalid token → 401
- A forged or expired Firebase token at `/auth/google` → 401, recorded as `login_failed`
- Each user only sees their own portfolios; another user's portfolio by id → 403
- `tests/test_accounts.py` covers the Google exchange and the `auth_events` rows (with the token check stubbed); the real token path was verified against the live site with a Firebase custom-token sign-in
