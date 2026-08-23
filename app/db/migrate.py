"""Programmatic Alembic entry point used by containers and tests."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import settings
from app.db.session import create_database_engine

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _migration_config() -> Config:
    migration_config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    migration_config.set_main_option(
        "script_location",
        str(_PROJECT_ROOT / "migrations"),
    )
    return migration_config


def upgrade_database(database_url: str | None = None) -> None:
    """Upgrade a database transactionally to the single current head."""

    migration_engine = create_database_engine(database_url or settings.database_url)
    migration_config = _migration_config()
    try:
        with migration_engine.begin() as connection:
            migration_config.attributes["connection"] = connection
            command.upgrade(migration_config, "head")
    finally:
        migration_engine.dispose()


def check_database_schema(database_url: str | None = None) -> None:
    """Fail when ORM metadata contains operations absent from migrations."""

    migration_engine = create_database_engine(database_url or settings.database_url)
    migration_config = _migration_config()
    try:
        with migration_engine.begin() as connection:
            migration_config.attributes["connection"] = connection
            command.check(migration_config)
    finally:
        migration_engine.dispose()


def main() -> None:
    upgrade_database()
    print("Database migration complete: head")


if __name__ == "__main__":
    main()
