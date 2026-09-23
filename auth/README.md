# Auth — Google Sign-In via Firebase

**Goal:** verify who a user is via their Google account, and make sure each user only ever sees their own portfolios — using Firebase instead of raw Google Cloud OAuth, to avoid Google's brand verification requirement.

**Depends on:** Part 3 (Paper Trading Engine) and Part 5 (Backend API) — this slots into both.

**Files in this folder:**
- `firebase_auth.py` — verifies Firebase's token; no separate session-token issuance needed, since a valid Firebase token is already a valid session

---

## Why Firebase instead of raw Google Cloud OAuth
Google's OAuth "brand verification" only applies once an app is published to Production for the general public. Firebase sidesteps this for a typical student/small project: enabling Google as a sign-in provider inside Firebase auto-configures everything without hitting that manual verification wall — no domain, no company registration needed. It also removes a layer of code: Firebase's token *is* the session, so there's no separate "issue our own JWT" step like the raw-OAuth version had.

## Before you start
- `pip install firebase-admin`
- A Google account (to create the free Firebase project)

## Firebase setup (one-time, do this first)
1. Go to **console.firebase.google.com** → **Create a project** (free — no domain or company needed; you can even skip Google Analytics for this project)
2. In the left sidebar: **Build → Authentication → Get started**
3. Under **Sign-in method**, click **Google**, toggle it **Enable**, pick a support email, **Save**
4. Click the gear icon → **Project settings → Service accounts**
5. Click **Generate new private key** — this downloads a JSON file. **Keep this private** — it's a real credential, never commit it to git.
6. Point the backend at it:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/your-serviceAccountKey.json"
   ```
7. From the same Project settings page, under **Your apps**, click the web icon (`</>`) to register a web app — this gives you a `firebaseConfig` object you'll need for the frontend snippet below.

## How the flow works
1. Frontend uses the **Firebase JS SDK** to show Google's sign-in popup
2. User signs in — Firebase handles the Google OAuth exchange internally, invisibly
3. Firebase SDK hands the frontend an ID token — a JWT, **auto-refreshed by the SDK** as it nears expiry, so you don't write any refresh logic yourself
4. Frontend attaches it to every API call: `Authorization: Bearer <token>`
5. The backend verifies it's genuinely from Firebase and not expired — no separate sign-in exchange endpoint needed; every request is checked directly
6. On a user's very first authenticated call, their local user record is created automatically

## Frontend integration snippet
Replace `firebaseConfig` with the values from your registered web app (Firebase project settings → Your apps):

```html
<script type="module">
  import { initializeApp } from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
  import { getAuth, GoogleAuthProvider, signInWithPopup, onAuthStateChanged }
    from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";

  const firebaseConfig = {
    apiKey: "YOUR_API_KEY",
    authDomain: "your-project.firebaseapp.com",
    projectId: "your-project-id",
    // ...the rest of the config Firebase gives you
  };

  const app = initializeApp(firebaseConfig);
  const auth = getAuth(app);

  async function signIn() {
    const result = await signInWithPopup(auth, new GoogleAuthProvider());
    const idToken = await result.user.getIdToken();
    localStorage.setItem("firebase_token", idToken);
    console.log("Signed in as", result.user.displayName);
  }

  // Keep the token fresh automatically — call this once when the page loads
  onAuthStateChanged(auth, async (user) => {
    if (user) {
      const freshToken = await user.getIdToken();
      localStorage.setItem("firebase_token", freshToken);
    }
  });

  document.getElementById("signin-button").addEventListener("click", signIn);
</script>
```

Then on every API call: `headers: { "Authorization": "Bearer " + localStorage.getItem("firebase_token") }`

## Checkpoint
Signing in through the Google popup returns a Firebase ID token; using that token, a user can create and view their own portfolios; a *different* signed-in user gets a clean 403 trying to access the first user's portfolio by ID — never their data.

## What was tested (and how, given this sandbox can't reach Firebase's servers)
The one piece requiring real network access (verifying a token's signature against Firebase's servers) was mocked in testing — everything downstream is real code that was actually executed and verified:
- No `Authorization` header → 401
- A garbage/invalid token → 401
- Two different signed-in users, each auto-created on their first call, each only sees their own portfolios
- User B requesting User A's portfolio by ID directly → 403
- Building the same portfolio twice → 409, not a silent double-spend

Before you deploy, do one real end-to-end test with your actual Firebase project and a real browser sign-in — that confirms the one piece that couldn't be tested here.

## Known limitations
- No explicit logout/revocation beyond what Firebase's SDK itself provides (`auth.signOut()` on the frontend stops the SDK from refreshing the token, but an already-issued token remains valid until its own ~1 hour expiry)
- The downloaded service account key is a powerful credential — treat it like a password; add its filename to `.gitignore`
