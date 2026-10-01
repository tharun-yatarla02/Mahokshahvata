"""Engine tests run against the local Firestore emulator, never the real
database. Start it first (it needs Java 21+):

    gcloud emulators firestore start --host-port=127.0.0.1:8681

Each engine gets its own project id, so tests never see each other's data.
"""
import os
import socket
import uuid

import pytest
from google.cloud import firestore

# Set before api.main is imported: it builds its engine (a Firestore client) at import.
os.environ["FIRESTORE_EMULATOR_HOST"] = os.environ.get("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8681")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

from paper_trading.paper_trading_engine import PaperTradingEngine  # noqa: E402


@pytest.fixture
def make_engine():
    host, port = os.environ["FIRESTORE_EMULATOR_HOST"].rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
    except OSError:
        pytest.fail("Firestore emulator isn't running: gcloud emulators firestore start --host-port=127.0.0.1:8681")
    return lambda: PaperTradingEngine(firestore.Client(project=f"test-{uuid.uuid4().hex[:12]}"))
