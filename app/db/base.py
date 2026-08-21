"""SQLAlchemy declarative base shared by all ORM models."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for ORM defaults."""

    return datetime.now(UTC)
