"""Disposable PostgreSQL database support for v2 integration tests."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

TEST_POSTGRES_ENV = "TEST_POSTGRES_URL"
_DATABASE_PREFIX = "thanh_v2_p2_"
_SAFE_PREFIX = re.compile(r"^thanh_v2_p2_[a-z0-9_]*$")


def require_test_postgres_url() -> URL:
    """Return the dedicated server URL or fail loudly with gate instructions."""

    raw_url = os.environ.get(TEST_POSTGRES_ENV)
    if not raw_url:
        raise RuntimeError(
            "PostgreSQL gate requires TEST_POSTGRES_URL pointing at the dedicated "
            "disposable test server; PostgreSQL tests are never silently skipped"
        )
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("TEST_POSTGRES_URL must use a PostgreSQL driver")
    if not url.database:
        raise RuntimeError("TEST_POSTGRES_URL must identify a baseline database")
    if url.database.startswith(_DATABASE_PREFIX):
        raise RuntimeError(
            "TEST_POSTGRES_URL must not target a disposable test database"
        )
    return url


@contextmanager
def disposable_postgres_database(
    prefix: str = "thanh_v2_p2_persistence_",
) -> Iterator[str]:
    """Create and clean one uniquely named DB without touching the baseline DB."""

    if not _SAFE_PREFIX.fullmatch(prefix) or len(prefix) > 31:
        raise ValueError("disposable database prefix must be scoped under thanh_v2_p2_")
    source_url = require_test_postgres_url()
    database_name = f"{prefix}{uuid4().hex}"
    maintenance_url = source_url.set(database="postgres")
    test_url = source_url.set(database=database_name)
    maintenance_engine = create_engine(
        maintenance_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    quoted_name = f'"{database_name}"'
    created = False
    try:
        with maintenance_engine.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            created = True
        yield test_url.render_as_string(hide_password=False)
    finally:
        if created:
            with maintenance_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        maintenance_engine.dispose()
