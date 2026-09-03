"""Shared SQLite fixture for database-independent tool tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import session as db_session
from app.db.base import Base
from app.db.seed import seed_database

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots" / "tiki-books-v4-test"


@pytest.fixture(autouse=True)
def seeded_test_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Point short-lived tool sessions at a fresh deterministic SQLite DB."""

    test_engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    test_session_factory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(db_session, "SessionLocal", test_session_factory)
    seed_database(
        snapshot_dir=TEST_SNAPSHOT_DIR,
        session_factory=test_session_factory,
    )
    yield
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()
