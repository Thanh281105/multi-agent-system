"""SQLAlchemy engine and short-lived session factory."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


def create_database_engine(database_url: str) -> Engine:
    """Create an engine without opening a database connection eagerly."""

    connect_args: dict[str, Any] = {}
    engine_options: dict[str, Any] = {}
    backend = make_url(database_url).get_backend_name()
    if backend == "sqlite":
        connect_args["check_same_thread"] = False
    elif backend == "postgresql":
        connect_args.update(
            connect_timeout=5,
            options="-c statement_timeout=10000 -c lock_timeout=5000",
        )
        engine_options["pool_timeout"] = 5

    return create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        **engine_options,
    )


engine = create_database_engine(settings.database_url)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield one database session and always close it after use.

    Read-only tools use this context manager as well. A rollback on failure
    keeps the same boundary safe if a future tool adds a write operation.
    """

    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
