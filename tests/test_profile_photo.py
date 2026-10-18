"""Profile photos: stored through the app's store (the bucket on the VM),
checked by content, private to their owner, replaced and removed cleanly."""
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from cloud.blobs import LocalStore

DEMO = {"Authorization": "Bearer demo-token"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def setup(make_engine, monkeypatch, tmp_path):
    monkeypatch.setenv("USE_DEMO_AUTH", "true")
    monkeypatch.setattr(api_main, "engine", make_engine())
    store = LocalStore(tmp_path)
    monkeypatch.setattr(api_main, "default_store", lambda: store)
    return TestClient(api_main.app), store, tmp_path


def upload(client, data, filename="me.png", content_type="image/png"):
    return client.post("/auth/me/photo", files={"photo": (filename, data, content_type)}, headers=DEMO)


def test_upload_stores_the_photo_and_serves_it_back(setup):
    client, store, root = setup
    res = upload(client, PNG)
    assert res.status_code == 200 and res.json()["has_photo"] is True
    stored = [p for p in root.rglob("*") if p.is_file()]
    assert len(stored) == 1 and stored[0].parent.parent.name == "avatars" and stored[0].read_bytes() == PNG
    photo = client.get("/auth/me/photo", headers=DEMO)
    assert photo.status_code == 200 and photo.content == PNG and photo.headers["content-type"] == "image/png"


def test_a_new_photo_replaces_the_old_object(setup):
    client, store, root = setup
    upload(client, PNG)
    upload(client, JPEG, "me.jpg", "image/jpeg")
    stored = [p for p in root.rglob("*") if p.is_file()]
    assert [p.suffix for p in stored] == [".jpg"]            # the PNG was deleted from storage
    assert client.get("/auth/me/photo", headers=DEMO).content == JPEG


def test_remove_deletes_it(setup):
    client, store, root = setup
    upload(client, PNG)
    assert client.delete("/auth/me/photo", headers=DEMO).json()["has_photo"] is False
    assert not [p for p in root.rglob("*") if p.is_file()]
    assert client.get("/auth/me/photo", headers=DEMO).status_code == 404


def test_rejects_non_images_by_content_and_oversized_files(setup):
    client, _, root = setup
    fake = upload(client, b"<script>alert(1)</script>", "evil.png", "image/png")   # claims PNG, isn't one
    big = upload(client, PNG + b"\x00" * (2 * 1024 * 1024))
    assert fake.status_code == 415 and big.status_code == 413
    assert not [p for p in root.rglob("*") if p.is_file()]


def test_photo_needs_sign_in(setup, monkeypatch):
    client, _, _ = setup
    assert client.get("/auth/me/photo").status_code == 401
    assert client.post("/auth/me/photo", files={"photo": ("a.png", PNG, "image/png")}).status_code == 401
