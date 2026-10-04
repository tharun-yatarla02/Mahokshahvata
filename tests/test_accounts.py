"""Email/password accounts: register, sign in, profile, sign out, and that
each account only ever sees its own portfolios."""
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from paper_trading.paper_trading_engine import PaperTradingEngine


@pytest.fixture
def engine(make_engine):
    return make_engine()


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    monkeypatch.setattr(api_main, "engine", engine)
    return TestClient(api_main.app)


def register(client, email="ada@example.com", password="analytical-engine", name="Ada Lovelace"):
    res = client.post("/auth/register", json={"name": name, "email": email, "password": password})
    assert res.status_code == 201, res.text
    return res.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_sign_in_and_use_the_app(client):
    token = register(client, email="  Ada@Example.COM ")

    me = client.get("/auth/me", headers=auth(token)).json()
    assert me["email"] == "ada@example.com"          # stored lowercased
    assert me["account_type"] == "email"
    starter = client.get("/portfolio", headers=auth(token)).json()["portfolios"]
    assert [(p["name"], p["cash_balance"]) for p in starter] == [(PaperTradingEngine.DEFAULT_PORTFOLIO_NAME, 100_000)]
    res = client.post("/portfolio", json={"name": "Mine", "starting_capital": 1000}, headers=auth(token))
    assert res.status_code == 200

    login = client.post("/auth/login", json={"email": "ADA@example.com", "password": "analytical-engine"})
    assert login.status_code == 200
    names = {p["name"] for p in client.get("/portfolio", headers=auth(login.json()["token"])).json()["portfolios"]}
    assert names == {PaperTradingEngine.DEFAULT_PORTFOLIO_NAME, "Mine"}


def test_login_errors_do_not_reveal_which_accounts_exist(client):
    register(client)
    wrong_password = client.post("/auth/login", json={"email": "ada@example.com", "password": "not-it-at-all"})
    unknown_email = client.post("/auth/login", json={"email": "nobody@example.com", "password": "not-it-at-all"})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": "Invalid email or password"}


def test_duplicate_email_and_weak_input_are_rejected(client):
    register(client)
    assert client.post("/auth/register", json={"name": "B", "email": "ADA@example.com", "password": "another-pass"}).status_code == 409
    assert client.post("/auth/register", json={"name": "C", "email": "c@example.com", "password": "short"}).status_code == 422
    assert client.post("/auth/register", json={"name": "D", "email": "not-an-email", "password": "long-enough"}).status_code == 422


def test_password_is_stored_hashed_and_session_token_is_not_stored(client, engine):
    token = register(client, password="plain-text-never")
    stored_password = engine._where("users", "email", "ada@example.com")[0]["password_hash"]
    stored_tokens = [s.id for s in engine.db.collection("sessions").stream()]
    assert stored_password.startswith("scrypt$") and "plain-text-never" not in stored_password
    assert stored_tokens and token not in stored_tokens


def test_logout_ends_the_session(client):
    token = register(client)
    assert client.post("/auth/logout", headers=auth(token)).status_code == 200
    assert client.get("/auth/me", headers=auth(token)).status_code == 401
    assert client.get("/portfolio").status_code == 401  # no token at all


def test_password_change_signs_out_other_devices(client):
    laptop = register(client, password="first-password")
    phone = client.post("/auth/login", json={"email": "ada@example.com", "password": "first-password"}).json()["token"]

    bad = client.post("/auth/password", json={"current_password": "wrong-one", "new_password": "second-password"}, headers=auth(laptop))
    assert bad.status_code == 400
    ok = client.post("/auth/password", json={"current_password": "first-password", "new_password": "second-password"}, headers=auth(laptop))
    assert ok.status_code == 200

    assert client.get("/auth/me", headers=auth(laptop)).status_code == 200   # this device stays in
    assert client.get("/auth/me", headers=auth(phone)).status_code == 401    # the other is signed out
    assert client.post("/auth/login", json={"email": "ada@example.com", "password": "second-password"}).status_code == 200


def test_profile_update(client):
    token = register(client)
    register(client, email="taken@example.com", name="Other")

    res = client.patch("/auth/me", json={"name": "  Ada   King ", "email": "ada.king@example.com"}, headers=auth(token))
    assert res.json()["name"] == "Ada King" and res.json()["email"] == "ada.king@example.com"
    assert client.patch("/auth/me", json={"name": "Ada", "email": "taken@example.com"}, headers=auth(token)).status_code == 409
    assert client.patch("/auth/me", json={"name": "   "}, headers=auth(token)).status_code == 422
    # Demo account's email comes from its provider and can't be changed here.
    demo = {"Authorization": "Bearer demo-token"}
    assert client.patch("/auth/me", json={"name": "Demo", "email": "x@example.com"}, headers=demo).status_code == 400


def test_accounts_cannot_see_each_others_portfolios(client):
    alice = register(client, email="alice@example.com")
    bob = register(client, email="bob@example.com")
    portfolio_id = client.post("/portfolio", json={"name": "Alice's", "starting_capital": 500}, headers=auth(alice)).json()["portfolio_id"]

    assert "Alice's" not in {p["name"] for p in client.get("/portfolio", headers=auth(bob)).json()["portfolios"]}
    assert client.get(f"/portfolio/{portfolio_id}", headers=auth(bob)).status_code == 403
    assert client.post(f"/portfolio/{portfolio_id}/cash", json={"amount": 10}, headers=auth(bob)).status_code == 403


def events(engine):
    rows = [e.to_dict() for e in engine.db.collection("auth_events").stream()]
    return sorted((e["event"], e["method"], e["user_id"] is not None) for e in rows)


def test_google_sign_in_gets_a_session_and_is_logged(client, engine, monkeypatch):
    def verify(token):
        if token != "good":
            raise ValueError("bad token")
        return {"firebase_uid": "g-123", "email": "grace@example.com", "name": "Grace Hopper", "picture": None}
    monkeypatch.setattr(api_main, "verify_firebase_token", verify)

    assert client.post("/auth/google", json={"id_token": "forged"}).status_code == 401
    first = client.post("/auth/google", json={"id_token": "good"}, headers={"X-Forwarded-For": "203.0.113.7"}).json()
    again = client.post("/auth/google", json={"id_token": "good"}).json()

    assert first["user"]["account_type"] == "google" and first["user"]["email"] == "grace@example.com"
    assert first["user"]["id"] == again["user"]["id"]          # same account on every sign-in
    assert client.get("/auth/me", headers=auth(first["token"])).status_code == 200
    assert client.post("/auth/logout", headers=auth(first["token"])).status_code == 200
    assert events(engine) == [("login", "google", True), ("login", "google", True),
                              ("login_failed", "google", False), ("logout", None, True)]
    ips = {e.get("ip") for e in (d.to_dict() for d in engine.db.collection("auth_events").stream())}
    assert "203.0.113.7" in ips


def test_email_sign_ins_are_logged(client, engine):
    register(client)
    client.post("/auth/login", json={"email": "ada@example.com", "password": "wrong-password"})
    client.post("/auth/login", json={"email": "ada@example.com", "password": "analytical-engine"})
    assert events(engine) == [("login", "email", True), ("login_failed", "email", True), ("register", "email", True)]


def google_as(monkeypatch, uid, email, verified=True):
    def verify(token):  # only "good" is a valid Google token, like the real check
        if token != "good":
            raise ValueError("bad token")
        return {"firebase_uid": uid, "email": email, "name": "Ada (Google)", "picture": None, "email_verified": verified}
    monkeypatch.setattr(api_main, "verify_firebase_token", verify)


def test_google_sign_in_joins_the_existing_account_for_that_email(client, engine, monkeypatch):
    old_token = register(client, email="ada@example.com", password="analytical-engine")
    user_id = client.get("/auth/me", headers=auth(old_token)).json()["id"]
    client.post("/portfolio", json={"name": "Mine", "starting_capital": 1000}, headers=auth(old_token))

    google_as(monkeypatch, "g-ada", "Ada@Example.com")
    session = client.post("/auth/google", json={"id_token": "good"}).json()

    assert session["user"]["id"] == user_id and session["user"]["account_type"] == "google"
    assert "Mine" in {p["name"] for p in client.get("/portfolio", headers=auth(session["token"])).json()["portfolios"]}
    assert len(engine._where("users", "email", "ada@example.com")) == 1
    # Whoever set the password never proved they own the email, so it stops working.
    assert client.get("/auth/me", headers=auth(old_token)).status_code == 401
    res = client.post("/auth/login", json={"email": "ada@example.com", "password": "analytical-engine"})
    assert res.status_code == 400 and "Google" in res.json()["detail"]


def test_email_used_by_a_google_account_cannot_be_reused(client, monkeypatch):
    google_as(monkeypatch, "g-grace", "grace@example.com")
    client.post("/auth/google", json={"id_token": "good"})

    res = client.post("/auth/register", json={"name": "G", "email": "grace@example.com", "password": "long-enough"})
    assert res.status_code == 409 and "Google" in res.json()["detail"]
    other = register(client, email="other@example.com")
    assert client.patch("/auth/me", json={"name": "O", "email": "grace@example.com"}, headers=auth(other)).status_code == 409


def test_unverified_google_email_does_not_take_over_an_account(client, monkeypatch):
    register(client, email="ada@example.com")
    google_as(monkeypatch, "g-someone", "ada@example.com", verified=False)
    assert client.post("/auth/google", json={"id_token": "good"}).status_code == 409
