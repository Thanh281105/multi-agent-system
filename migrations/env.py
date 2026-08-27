"""Alembic environment backed by the application's validated DB settings."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from app.core.config import settings
from app.db.base import Base
from app.db.session import create_database_engine
from app.models import Product, Review, Shop

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Importing model classes registers their tables on Base.metadata.
_registered_models = (Product, Review, Shop)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without opening a database connection."""

    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations using a shared programmatic or configured connection."""

    supplied_connection = config.attributes.get("connection")
    if isinstance(supplied_connection, Connection):
        _run_with_connection(supplied_connection)
        return

    migration_engine = create_database_engine(settings.database_url)
    try:
        with migration_engine.connect() as connection:
            _run_with_connection(connection)
    finally:
        migration_engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
