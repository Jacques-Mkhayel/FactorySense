"""Shared async connection pool. Connects as the least-privilege `api` role (030-auth.sh)."""
import os

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

CONNINFO = make_conninfo(
    host=os.getenv("POSTGRES_HOST", "timescaledb"),
    port=os.getenv("POSTGRES_PORT", "5432"),
    dbname=os.getenv("POSTGRES_DB", "factorysense"),
    user=os.getenv("POSTGRES_USER", "api"),
    password=os.getenv("POSTGRES_PASSWORD", ""),
)

# Opened in the app lifespan. Autocommit: every endpoint is one short statement or a few independent ones.
pool = AsyncConnectionPool(CONNINFO, min_size=1, max_size=10, open=False,
                           kwargs={"autocommit": True, "row_factory": dict_row})
