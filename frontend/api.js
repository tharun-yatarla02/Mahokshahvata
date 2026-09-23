// Shared fetch helper for every dashboard page.
// ponytail: always sends the demo token; wire Firebase sign-in here (and only here)
// by swapping getAuthToken() for the signed-in user's ID token.
function getAuthToken() {
  return 'demo-token';
}

async function apiFetch(path, options = {}) {
  const res = await fetch(`${window.location.origin}${path}`, {
    cache: 'no-store',
    ...options,
    headers: {
      Authorization: `Bearer ${getAuthToken()}`,
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  const text = await res.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  if (!res.ok) throw new Error(payload?.detail || payload || `HTTP ${res.status}`);
  return payload;
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
