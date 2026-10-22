"""
connection.py

Opens connections to the app's PostgreSQL database (Cloud SQL on the VM, a
local container on laptops and in CI). Settings come from the environment:

    DB_HOST      Cloud SQL's private IP on the VM; localhost locally
    DB_PORT      default 5432
    DB_NAME      default mahokshahvata
    DB_USER      default app
    DB_PASSWORD  from Secret Manager on the VM (cloud/secrets.py); unset locally,
                 where the dev container trusts local connections
    DB_SSLMODE   "require" on the VM (Cloud SQL enforces TLS); default "prefer"

Rows come back as plain dicts with JSON-friendly values: NUMERIC as float,
timestamps and dates as ISO strings. That is the shape the rest of the app
already used with Firestore, so nothing above the engine had to change.
"""

import os
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.numeric import FloatLoader


def conninfo(**overrides):
    settings = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "5432"),
        "dbname": os.environ.get("DB_NAME", "mahokshahvata"),
        "user": os.environ.get("DB_USER", "app"),
        "sslmode": os.environ.get("DB_SSLMODE", "prefer"),
        "connect_timeout": "10",
        "application_name": "mahokshahvata",
        # TCP keepalives: the app keeps one long-lived connection, and an idle one to
        # Cloud SQL can otherwise be cut silently (seen as "SSL error: unexpected eof").
        "keepalives": "1",
        "keepalives_idle": "60",
        "keepalives_interval": "10",
        "keepalives_count": "3",
        **overrides,
    }
    password = os.environ.get("DB_PASSWORD")
    if password:
        settings["password"] = password
    return psycopg.conninfo.make_conninfo(**settings)


def _plain(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def plain_rows(cursor):
    """Row factory: dicts with ISO strings for timestamps and floats for numbers."""
    make_dict = dict_row(cursor)

    def make_row(values):
        return {key: _plain(value) for key, value in make_dict(values).items()}
    return make_row


def connect(schema=None, **overrides):
    """A new autocommit connection; use conn.transaction() for multi-statement
    writes. schema: put this schema first on the search path (tests give each
    engine its own, so they never see each other's rows)."""
    options = "-c timezone=UTC" + (f" -c search_path={schema}" if schema else "")
    conn = psycopg.connect(conninfo(options=options, **overrides), autocommit=True, row_factory=plain_rows)
    conn.adapters.register_loader("numeric", FloatLoader)
    return conn
