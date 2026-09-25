import os

import pytest
from fastapi import HTTPException

from api.main import get_current_user
from auth.firebase_auth import verify_firebase_token


def test_demo_auth_accepts_local_dev_token(monkeypatch):
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    result = verify_firebase_token("demo-token")

    assert result["firebase_uid"] == "demo-token"
    assert result["email"] == "demo@example.com"
    assert result["name"] == "Local Demo User"


def test_demo_auth_rejects_invalid_token(monkeypatch):
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    with pytest.raises(ValueError):
        verify_firebase_token("")


def test_missing_header_is_rejected_even_in_demo_mode(monkeypatch):
    # A signed-out browser sends no token; it must not land in the demo
    # account (and see its portfolios) just because demo mode is on.
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    with pytest.raises(HTTPException) as err:
        get_current_user(None)
    assert err.value.status_code == 401


def test_demo_auth_rejects_other_tokens(monkeypatch):
    # Any other string must not become a user id: that would let a caller
    # sign in as anyone by sending their Firebase uid.
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    with pytest.raises(ValueError):
        verify_firebase_token("some-real-users-firebase-uid")


def test_missing_firebase_config_is_an_auth_error_not_a_crash(monkeypatch):
    import auth.firebase_auth as fa
    monkeypatch.delenv("USE_DEMO_AUTH", raising=False)

    def not_configured():
        raise RuntimeError("Your default credentials were not found")
    monkeypatch.setattr(fa, "_get_app", not_configured)

    with pytest.raises(ValueError, match="not configured"):
        verify_firebase_token("anything")
