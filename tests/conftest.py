"""Engine tests run against a local PostgreSQL, never the real database.
Start one first (local connections are trusted, so there is no password):

    docker run -d --name mahokshahvata-pg -p 127.0.0.1:5433:5432 \
      -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=app -e POSTGRES_DB=mahokshahvata postgres:16-alpine

Each engine gets its own schema with the migrations applied, so tests never
see each other's rows; the schemas are dropped when the run ends.
"""
import os
import sys
import uuid

import psycopg
import pytest

# Set before api.main is imported. CI's Postgres listens on 5432.
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5433")
os.environ.pop("DB_PASSWORD", None)

from db.connection import connect  # noqa: E402
from db.migrate import migrate  # noqa: E402
from paper_trading.paper_trading_engine import PaperTradingEngine  # noqa: E402

_schemas = []


@pytest.fixture(scope="session")
def _database():
    try:
        conn = connect()
    except psycopg.OperationalError as e:
        pytest.fail(f"PostgreSQL isn't running on {os.environ['DB_HOST']}:{os.environ['DB_PORT']} (see tests/conftest.py): {e}")
    yield conn
    for schema in _schemas:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.close()


@pytest.fixture
def make_engine(_database):
    def make():
        schema = f"test_{uuid.uuid4().hex[:12]}"
        _database.execute(f"CREATE SCHEMA {schema}")
        _schemas.append(schema)
        with connect(schema=schema) as conn:
            migrate(conn)
        return PaperTradingEngine(lambda: connect(schema=schema))
    return make


@pytest.fixture(autouse=True)
def _fresh_login_limiter():
    """Every test client signs in from the same address, so failed logins in
    one test mustn't throttle the next."""
    api_main = sys.modules.get("api.main")
    if api_main:
        api_main.login_limiter.reset()
