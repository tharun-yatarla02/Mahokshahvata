// Shared fetch helper and sign-in state for every dashboard page.
//
// Sign-in: /auth/login, /auth/register or /auth/google return a session token, kept in
// localStorage and sent as "Authorization: Bearer <token>". The server stores
// only a hash of it, and /auth/logout deletes it. "demo-token" is the shared
// demo account (server must run with USE_DEMO_AUTH=true).
const TOKEN_KEY = 'mahokshahvata_session';
const PUBLIC_PAGES = ['/login.html'];

function getAuthToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

function setAuthToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* storage blocked: the session just won't survive a reload */ }
}

function goToLogin() {
  const next = window.location.pathname + window.location.search;
  window.location.replace(`/login.html?next=${encodeURIComponent(next)}`);
}

// Where to go after signing in: only pages on this site, so ?next= can't
// send users elsewhere. Parsed rather than prefix-checked, because browsers
// read "/\evil.com" as "//evil.com".
function safeNextPath(value) {
  try {
    const url = new URL(String(value || '/'), window.location.origin);
    return url.origin === window.location.origin ? url.pathname + url.search + url.hash : '/';
  } catch {
    return '/';
  }
}

// Signed-out visitors can't use the dashboard pages at all.
if (!getAuthToken() && !PUBLIC_PAGES.includes(window.location.pathname)) goToLogin();

async function apiFetch(path, options = {}) {
  const token = getAuthToken();
  const res = await fetch(`${window.location.origin}${path}`, {
    cache: 'no-store',
    ...options,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  const text = await res.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  // Expired or revoked session (e.g. password changed elsewhere): sign in again.
  if (res.status === 401 && !path.startsWith('/auth/login') && !PUBLIC_PAGES.includes(window.location.pathname)) {
    setAuthToken(null);
    goToLogin();
  }
  if (!res.ok) {
    const error = new Error(errorMessage(payload) || `HTTP ${res.status}`);
    error.status = res.status;  // lets callers react to e.g. 409 "email taken"
    throw error;
  }
  return payload;
}

// The portfolio every page works with: the user's most recent one, created
// with $100,000 on first visit (the server makes sure only one gets created).
async function getPortfolioId() {
  const data = await apiFetch('/portfolio/default', { method: 'POST' });
  return data.portfolio_id;
}

async function signOut() {
  try { await apiFetch('/auth/logout', { method: 'POST' }); } catch { /* signing out anyway */ }
  setAuthToken(null);
  window.location.replace('/login.html');
}

function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : parts[0].slice(0, 2)).toUpperCase();
}

// The account menu in every page's navbar: real name/email from /auth/me.
async function initAccountMenu() {
  const button = document.getElementById('profileButton');
  const menu = document.getElementById('profileMenu');
  if (!button || !menu) return;
  button.addEventListener('click', () => menu.classList.toggle('open'));
  document.addEventListener('click', (event) => {
    if (!button.contains(event.target) && !menu.contains(event.target)) menu.classList.remove('open');
  });
  document.getElementById('accountButton')?.addEventListener('click', () => { window.location.href = '/profile.html'; });
  document.getElementById('logoutButton')?.addEventListener('click', signOut);
  await refreshAccountMenu();
}

// Fills the menu from /auth/me; call again after the profile changes.
async function refreshAccountMenu() {
  const button = document.getElementById('profileButton');
  if (!button) return;
  try {
    const user = await apiFetch('/auth/me');
    const name = user.name || user.email || 'Account';
    button.querySelector('.profile-avatar').textContent = initials(name);
    document.getElementById('profileLabel').textContent = name.split(' ')[0];
    document.getElementById('profileMenuAvatar').textContent = initials(name);
    document.getElementById('profileName').textContent = name;
    document.getElementById('profileEmail').textContent = user.email || '';
  } catch { /* a 401 already redirected to sign-in */ }
}

if (getAuthToken()) {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initAccountMenu);
  else initAccountMenu();
}

// FastAPI sends validation errors (422) as a list of {loc, msg}; show those
// as text instead of "[object Object]".
function errorMessage(payload) {
  const detail = payload?.detail ?? payload;
  if (Array.isArray(detail)) {
    return detail.map((err) => {
      const field = (err.loc || []).filter((part) => part !== 'body').join('.');
      return field ? `${field}: ${err.msg}` : err.msg;
    }).join('; ');
  }
  return typeof detail === 'string' ? detail : '';
}

// Anything from a feed or the API goes through these before innerHTML.
function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// Only http(s) links; escaping alone would still let a javascript: URL through.
function safeUrl(value) {
  if (!value) return '';
  try {
    const url = new URL(String(value || ''), window.location.origin);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
  } catch {
    return '';
  }
}
