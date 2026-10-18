"""Secret Manager loading: fills the environment from the latest secret
versions, skips optional secrets that don't exist, and does nothing without
SECRETS_PROJECT."""
import os
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound, PermissionDenied

from cloud.secrets import load_secrets


class FakeClient:
    def __init__(self, values, denied=()):
        self.values, self.denied, self.asked = values, set(denied), []

    def access_secret_version(self, name):
        self.asked.append(name)
        secret = name.split("/")[3]
        if secret in self.denied:
            raise PermissionDenied("no access")  # what Secret Manager says for a secret you can't see
        if secret not in self.values:
            raise NotFound("no such secret")
        return SimpleNamespace(payload=SimpleNamespace(data=self.values[secret].encode()))


def test_loads_existing_secrets_and_skips_missing_ones(monkeypatch):
    # setenv first so monkeypatch restores (removes) whatever load_secrets writes
    for var in ("DB_PASSWORD", "ANTHROPIC_API_KEY", "QUIVER_API_KEY"):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    client = FakeClient({"db-password": "pw-from-sm\n", "anthropic-api-key": "sk-test"})
    loaded = load_secrets("my-project", client)
    assert loaded == ["DB_PASSWORD", "ANTHROPIC_API_KEY"]
    assert os.environ["DB_PASSWORD"] == "pw-from-sm" and os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert "QUIVER_API_KEY" not in os.environ
    assert client.asked[0] == "projects/my-project/secrets/db-password/versions/latest"


def test_does_nothing_without_a_project(monkeypatch):
    monkeypatch.delenv("SECRETS_PROJECT", raising=False)
    assert load_secrets(client=FakeClient({})) == []


def test_optional_secrets_without_access_are_skipped_but_the_db_password_is_required(monkeypatch):
    for var in ("DB_PASSWORD", "QUIVER_API_KEY"):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    assert load_secrets("p", FakeClient({"db-password": "pw"}, denied={"quiver-api-key"})) == ["DB_PASSWORD"]
    with pytest.raises(PermissionDenied):
        load_secrets("p", FakeClient({}, denied={"db-password"}))
