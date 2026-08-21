"""Shared SQLite fixture for database-independent tool tests."""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import session as db_session
from app.db.base import Base
from app.db.seed import seed_database


@pytest.fixture(autouse=True)
def seeded_test_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point short-lived tool sessions at a fresh deterministic SQLite DB."""

    test_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_session_factory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    monkeypatch.setattr(db_session, "SessionLocal", test_session_factory)
    seed_database(db_engine=test_engine, session_factory=test_session_factory)
    yield
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()
