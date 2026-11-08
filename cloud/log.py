"""One JSON object per line on stdout. GKE's logging agent turns each line into
a structured Cloud Logging entry: "severity" becomes the entry's severity,
"message" its summary, and the other keys land in jsonPayload (so the 5xx
log-based metric can filter on jsonPayload.status, terraform/monitoring.tf).
uvicorn's own loggers use this too (uvicorn_log.json)."""

import json
import logging
import os

VERSION = os.environ.get("GIT_SHA", "dev")  # baked into the image by the pipeline


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name, "version": VERSION}
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def log(severity, message, **fields):
    """log("WARNING", "news unavailable", error="Timeout")"""
    logging.getLogger("app").log(getattr(logging, severity), message, extra={"fields": fields})
