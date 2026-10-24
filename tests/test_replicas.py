"""Running as several replicas: only one runs the autopilot scheduler at a
time, and the two health checks mean different things."""
import psycopg
from fastapi.testclient import TestClient

import api.main as api_main


def test_only_one_replica_runs_the_scheduler(_database):
    with api_main._only_one_replica() as first:
        with api_main._only_one_replica() as second:  # another pod, same moment
            assert first is True and second is False
    with api_main._only_one_replica() as later:  # released when the first finished
        assert later is True


def test_liveness_ignores_the_database_and_readiness_does_not(_database, monkeypatch):
    client = TestClient(api_main.app)
    assert client.get("/ready").json() == {"status": "ready"}

    def no_database(**kwargs):
        raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(api_main, "db_connect", no_database)
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
