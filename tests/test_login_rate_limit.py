"""Sign-in throttling: repeated failed logins for one email or from one IP
are refused with 429 until the window passes."""
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from auth.rate_limit import LoginRateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_email_is_blocked_after_its_limit_and_unblocks_when_failures_age_out():
    clock = FakeClock()
    limiter = LoginRateLimiter(window=600, per_email=3, per_ip=100, clock=clock)
    for _ in range(3):
        assert limiter.retry_after("a@x.com", "1.1.1.1") == 0
        limiter.record_failure("a@x.com", "1.1.1.1")
        clock.now += 10
    assert 0 < limiter.retry_after("a@x.com", "2.2.2.2") <= 600  # blocked from any address
    assert limiter.retry_after("b@x.com", "1.1.1.1") == 0          # other accounts aren't
    clock.now = 1000 + 600                                         # the first failure ages out
    assert limiter.retry_after("a@x.com", "1.1.1.1") == 0


def test_ip_is_blocked_after_failing_on_many_emails():
    limiter = LoginRateLimiter(per_email=100, per_ip=3, clock=FakeClock())
    for i in range(3):
        limiter.record_failure(f"user{i}@x.com", "1.1.1.1")
    assert limiter.retry_after("someone-new@x.com", "1.1.1.1") > 0
    assert limiter.retry_after("someone-new@x.com", "9.9.9.9") == 0


def test_success_clears_the_email_but_not_the_ip():
    limiter = LoginRateLimiter(per_email=2, per_ip=3, clock=FakeClock())
    limiter.record_failure("a@x.com", "1.1.1.1")
    limiter.record_success("a@x.com")
    limiter.record_failure("a@x.com", "1.1.1.1")
    assert limiter.retry_after("a@x.com", None) == 0
    limiter.record_failure("b@x.com", "1.1.1.1")
    assert limiter.retry_after("c@x.com", "1.1.1.1") > 0


@pytest.fixture
def client(make_engine, monkeypatch):
    monkeypatch.setattr(api_main, "engine", make_engine())
    return TestClient(api_main.app)


def login(client, password, email="ada@example.com"):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_login_returns_429_after_five_wrong_passwords_even_with_the_right_one(client):
    client.post("/auth/register", json={"name": "Ada", "email": "ada@example.com", "password": "analytical-engine"})
    for _ in range(5):
        assert login(client, "wrong-guess").status_code == 401
    blocked = login(client, "analytical-engine")
    assert blocked.status_code == 429
    assert blocked.json()["detail"].startswith("Too many sign-in attempts. Try again in 15 minutes")
    assert 0 < int(blocked.headers["Retry-After"]) <= 15 * 60
    assert login(client, "x", email="ADA@Example.com ").status_code == 429  # same account, other spelling


def test_a_successful_login_resets_the_count(client):
    client.post("/auth/register", json={"name": "Ada", "email": "ada@example.com", "password": "analytical-engine"})
    for _ in range(4):
        login(client, "wrong-guess")
    assert login(client, "analytical-engine").status_code == 200
    for _ in range(4):
        assert login(client, "wrong-guess").status_code == 401
    assert login(client, "analytical-engine").status_code == 200
