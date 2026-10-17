"""
rate_limit.py

Throttles password guessing on POST /auth/login. Failed attempts are counted
per email and per client IP over a sliding window; once either key reaches its
limit, sign-in is refused (429) until the oldest failure ages out, even with
the right password. A successful sign-in clears that email's failures.

    - Per email: stops guessing one account's password from many addresses.
    - Per IP: stops one address trying a few passwords on many accounts.

Kept in memory, which fits the single uvicorn process this app runs as
(see README, "One server process"); a restart forgets all failures.
"""

import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 20


class LoginRateLimiter:
    def __init__(self, window=WINDOW_SECONDS, per_email=MAX_FAILURES_PER_EMAIL,
                 per_ip=MAX_FAILURES_PER_IP, clock=time.monotonic):
        self.window, self.clock = window, clock
        self.limits = {"email": per_email, "ip": per_ip}
        self._failures = defaultdict(deque)  # (kind, value) -> failure times, oldest first
        self._lock = threading.Lock()

    def _keys(self, email, ip):
        return [(kind, value) for kind, value in (("email", email), ("ip", ip)) if value]

    def _prune(self, key, now):
        times = self._failures.get(key)
        while times and now - times[0] >= self.window:
            times.popleft()
        if key in self._failures and not times:
            del self._failures[key]  # don't keep an entry per address ever seen

    def retry_after(self, email, ip):
        """Seconds until sign-in is allowed again, or 0 if it's allowed now."""
        now = self.clock()
        wait = 0
        with self._lock:
            for key in self._keys(email, ip):
                self._prune(key, now)
                times = self._failures.get(key)
                if times and len(times) >= self.limits[key[0]]:
                    # Allowed again once enough failures age out to drop below the limit.
                    unblock_at = times[len(times) - self.limits[key[0]]] + self.window
                    wait = max(wait, unblock_at - now)
        return int(wait) + 1 if wait else 0

    def record_failure(self, email, ip):
        now = self.clock()
        with self._lock:
            for key in self._keys(email, ip):
                self._prune(key, now)
                self._failures[key].append(now)

    def record_success(self, email):
        with self._lock:
            self._failures.pop(("email", email), None)

    def reset(self):
        with self._lock:
            self._failures.clear()
