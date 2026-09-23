import os

import pytest

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


def test_demo_auth_missing_header_falls_back_to_demo_user(monkeypatch):
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    user_id = get_current_user(None)

    assert isinstance(user_id, int)
    assert user_id > 0
