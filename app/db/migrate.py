"""Programmatic Alembic entry point used by containers and tests."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import settings
from app.db.session import create_database_engine

EXPECTED_DATABASE_REVISION = "20260830_0003"


def _migration_root() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file() and (
            candidate / "migrations"
        ).is_dir():
            return candidate
    raise RuntimeError("alembic.ini and migrations directory are unavailable")


def _migration_config() -> Config:
    project_root = _migration_root()
    migration_config = Config(str(project_root / "alembic.ini"))
    migration_config.set_main_option(
        "script_location",
        str(project_root / "migrations"),
    )
    return migration_config


def upgrade_database(
    database_url: str | None = None,
    *,
    revision: str = "head",
) -> None:
    """Upgrade to a revision and verify SQLite batch-migration foreign keys."""

    migration_engine = create_database_engine(database_url or settings.database_url)
    migration_config = _migration_config()
    try:
        if migration_engine.dialect.name == "sqlite":
            with migration_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
                migration_config.attributes["connection"] = connection
                command.upgrade(migration_config, revision)
                connection.commit()
                violations = connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                connection.commit()
                if violations:
                    raise RuntimeError(
                        "database migration introduced foreign key violations"
                    )
            return
        with migration_engine.begin() as connection:
            migration_config.attributes["connection"] = connection
            command.upgrade(migration_config, revision)
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
