"""Database migration checks against an isolated SQLite database."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db.migrate import check_database_schema, upgrade_database


def test_initial_migration_reaches_head_and_is_idempotent(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'migration.db').as_posix()}"

    upgrade_database(database_url)
    upgrade_database(database_url)
    check_database_schema(database_url)

    migration_engine = create_engine(database_url)
    try:
        assert set(inspect(migration_engine).get_table_names()) == {
            "alembic_version",
            "products",
            "reviews",
            "shops",
        }
        with migration_engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert revision == "20260824_0001"
    finally:
        migration_engine.dispose()
