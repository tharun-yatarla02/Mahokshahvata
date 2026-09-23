# Auth — Google Sign-In

**Goal:** verify who a user is via their Google account, and make sure each user only ever sees their own portfolios.

**Depends on:** Part 3 (Paper Trading Engine) and Part 5 (Backend API) — this slots into both.

**Files in this folder:**
- `google_auth.py` — verifies Google's token, issues our own session token

---

## Before you start
- A Google account (to create the OAuth credentials — this is free)
- `pip install google-auth pyjwt`

## Google Cloud setup (one-time, do this first)
1. Go to **console.cloud.google.com** → APIs & Services → Credentials
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Web application**
4. Under "Authorized JavaScript origins," add every URL your frontend will run on — e.g. `http://localhost:5500` for local development, and your real deployed URL once Part 7 is done
5. Click Create. Copy the **Client ID** it gives you (looks like `123456-abc.apps.googleusercontent.com`) — you do **not** need the client secret for this flow
6. Set two environment variables before running the backend:
   ```bash
   export GOOGLE_CLIENT_ID="your-id.apps.googleusercontent.com"
   export SESSION_SECRET="any-long-random-string-keep-this-private"
   ```

## How the flow works
1. Frontend renders Google's official Sign-In button (their JS library, not custom code)
2. User signs in with their Google account — **your backend never sees their Google password**
3. Google hands the *frontend* a signed ID token proving who the user is
4. Frontend sends that token to `POST /auth/google`
5. The backend verifies it really came from Google and was issued for *this* app (checks the signature and the "audience"), then looks up or creates a local user record
6. Backend issues its **own** session token back to the frontend
7. Frontend attaches that session token to every future API call: `Authorization: Bearer <token>`
8. Every portfolio endpoint now requires that header — no token, no access; someone else's token, no access to *your* portfolios

## Frontend integration snippet
Add this to the dashboard's HTML, replacing `YOUR_CLIENT_ID`:

```html
<script src="https://accounts.google.com/gsi/client" async defer></script>
<div id="g_id_onload"
     data-client_id="YOUR_CLIENT_ID.apps.googleusercontent.com"
     data-callback="handleGoogleSignIn">
</div>
<div class="g_id_signin" data-type="standard"></div>

<script>
async function handleGoogleSignIn(response) {
  // response.credential is the Google ID token
  const res = await fetch("http://localhost:8000/auth/google", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: response.credential })
  });
  const data = await res.json();
  // Store this and attach it as "Authorization: Bearer <token>" on every future API call
  localStorage.setItem("session_token", data.session_token);
  console.log("Signed in as", data.user.name);
}
</script>
```

## Checkpoint
Signing in through Google's button returns a session token; using that token, a user can create and view their own portfolios; a *different* signed-in user gets a clean 403 trying to access the first user's portfolio by ID — never their data.

## What was tested (and how, given this sandbox can't reach Google's servers)
The one piece requiring real network access to Google (verifying an ID token's signature) was mocked in testing — everything downstream of that (session token issuance, user creation, per-user portfolio scoping, the 401/403 error handling) is real code that was actually executed and verified:
- No `Authorization` header → 401
- A garbage/invalid session token → 401
- Two different signed-in users, each creating their own portfolio → each only sees their own in `/portfolio`
- User B requesting User A's portfolio by ID directly → 403, not the data

Before you deploy, do one real end-to-end test with your actual Google Client ID and a real browser sign-in — that confirms the one piece that couldn't be tested here.

## Known limitations
- Session tokens last 7 days and there's no logout/revocation endpoint yet — signing out is currently just "the frontend deletes its stored token," which doesn't invalidate it server-side. Add a revocation list or shorter-lived tokens with refresh if that matters for your use case.
- `SESSION_SECRET` must be kept private and set to something random before deploying — the default in the code is only for local development and is not safe to use in production.
