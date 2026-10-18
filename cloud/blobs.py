"""
blobs.py

Where the app keeps files: a private Cloud Storage bucket on the VM, a local
folder everywhere else. The VM reaches the bucket as its own service account
(Application Default Credentials from the metadata server), so there is no
key file anywhere.

    DATA_BUCKET  bucket name; when set, files go to Cloud Storage
    DATA_DIR     local folder otherwise (default: <repo>/.data)

Objects: market_cache.json (momentum/market_data.py), sentiment_history.json
(news_sentiment/news_sentiment_scraper.py) and avatars/<user>/<id>.<ext>
(profile photos, api/main.py).
"""

import os
from pathlib import Path


class LocalStore:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, name):
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents:  # names come from the app, but never escape the folder
            raise ValueError(f"Bad object name: {name}")
        return path

    def read(self, name):
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, name, data, content_type="application/octet-stream"):
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # readers never see a half-written file

    def delete(self, name):
        self._path(name).unlink(missing_ok=True)

    def __repr__(self):
        return f"LocalStore({self.root})"


class GCSStore:
    def __init__(self, bucket_name):
        from google.cloud import storage  # only needed on the VM
        self.bucket = storage.Client().bucket(bucket_name)

    def read(self, name):
        from google.api_core.exceptions import NotFound
        try:
            return self.bucket.blob(name).download_as_bytes()
        except NotFound:
            return None

    def write(self, name, data, content_type="application/octet-stream"):
        self.bucket.blob(name).upload_from_string(data, content_type=content_type)

    def delete(self, name):
        from google.api_core.exceptions import NotFound
        try:
            self.bucket.blob(name).delete()
        except NotFound:
            pass

    def __repr__(self):
        return f"GCSStore(gs://{self.bucket.name})"


_default = None


def default_store():
    """The app's store, chosen once from DATA_BUCKET / DATA_DIR."""
    global _default
    if _default is None:
        bucket = os.environ.get("DATA_BUCKET")
        _default = GCSStore(bucket) if bucket else LocalStore(
            os.environ.get("DATA_DIR") or Path(__file__).parent.parent / ".data")
    return _default
