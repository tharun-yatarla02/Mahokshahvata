"""Request logs are JSON lines, and a 5xx is logged with status 500 (what the
log-based metric in terraform/monitoring.tf counts)."""
import json
import logging

from fastapi.testclient import TestClient

import api.main as api_main
from cloud.log import JsonFormatter


def test_controlled_500_is_logged_as_json(caplog):
    client = TestClient(api_main.app, raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="app"):
        assert client.get("/debug/fail").status_code == 500
    entry = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert entry["severity"] == "ERROR" and entry["status"] == 500 and entry["path"] == "/debug/fail"
    assert "version" in client.get("/health").json()
