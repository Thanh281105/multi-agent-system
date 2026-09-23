"""Database migration checks against isolated SQLite databases."""

from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.db.base import Base
from app.db.migrate import (
    EXPECTED_DATABASE_REVISION,
    _migration_config,
    check_database_schema,
    upgrade_database,
)
from tests.v2_postgres_support import disposable_postgres_database


def test_initial_migration_reaches_head_and_matches_orm(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path, "migration.db")

    upgrade_database(database_url)
    upgrade_database(database_url)
    check_database_schema(database_url)

    migration_engine = create_engine(database_url)
    try:
        inspector = inspect(migration_engine)
        assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {
            "alembic_version"
        }
        with migration_engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert revision == EXPECTED_DATABASE_REVISION

        product_columns = {
            column["name"]: column for column in inspector.get_columns("products")
        }
        review_columns = {
            column["name"]: column for column in inspector.get_columns("reviews")
        }
        source_columns = {
            column["name"]: column
            for column in inspector.get_columns("dataset_sources")
        }
        for column_name in (
            "authors",
            "publisher",
            "page_count",
            "source_review_count",
            "cover_url",
            "seller_name",
            "source_metadata",
        ):
            assert column_name in product_columns
        assert product_columns["shop_id"]["nullable"] is True
        assert product_columns["original_price"]["nullable"] is True
        assert product_columns["rating"]["nullable"] is True
        assert product_columns["sold_count"]["nullable"] is True
        assert product_columns["category"]["type"].length == 120
        assert review_columns["external_id"]["type"].length == 257
        assert review_columns["created_at"]["nullable"] is True
        assert review_columns["created_at"]["default"] is None
        assert review_columns["helpful_count"]["nullable"] is False
        assert source_columns["profile"]["nullable"] is False

        shop_fk = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys("products")
            if foreign_key["constrained_columns"] == ["shop_id"]
        )
        assert shop_fk["options"].get("ondelete") == "SET NULL"
        identity = next(
            constraint
            for constraint in inspector.get_unique_constraints("dataset_sources")
            if constraint["name"] == "uq_dataset_sources_identity_profile"
        )
        assert identity["column_names"] == [
            "dataset_id",
            "dataset_version",
            "profile",
        ]
    finally:
        migration_engine.dispose()


def test_upgrade_from_provenance_schema_preserves_legacy_rows(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, "legacy.db")
    engine = create_engine(database_url)
    try:
        upgrade_database(database_url, revision="20260824_0002")
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(
                text(
                    "INSERT INTO dataset_sources ("
                    "id, dataset_id, dataset_version, source_url, source_license, "
                    "source_revision, raw_archive_sha256, snapshot_sha256, "
                    "sampling_seed, product_count, review_count, retrieved_at"
                    ") VALUES ("
                    "1, 'legacy-dataset', 'v1', 'https://example.test/legacy', "
                    "'CC0-1.0', 1, :raw_hash, :snapshot_hash, 42, 1, 1, "
                    ":retrieved_at)"
                ),
                {
                    "raw_hash": "a" * 64,
                    "snapshot_hash": "b" * 64,
                    "retrieved_at": "2025-01-01 00:00:00",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO shops (id, name, platform, rating) "
                    "VALUES (7, 'Legacy shop', 'Tiki', 4.5)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO products ("
                    "id, source_id, external_id, shop_id, name, category, price, "
                    "original_price, rating, sold_count, description, platform"
                    ") VALUES ("
                    "11, 1, 'legacy-book', 7, 'Legacy title', 'Văn học', 90000, "
                    "100000, 4.0, 5, 'Legacy description', 'Tiki')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO reviews ("
                    "id, source_id, external_id, product_id, rating, content, "
                    "created_at"
                    ") VALUES ("
                    "13, 1, 'legacy-review', 11, 5, 'Legacy review', :created_at)"
                ),
                {"created_at": "2025-01-02 00:00:00"},
            )

        upgrade_database(database_url)

        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            source = connection.execute(
                text(
                    "SELECT id, dataset_id, profile, cleaner_version "
                    "FROM dataset_sources"
                )
            ).one()
            product = connection.execute(
                text(
                    "SELECT id, name, shop_id, original_price, rating, sold_count, "
                    "authors, source_review_count, source_metadata "
                    "FROM products"
                )
            ).one()
            review = connection.execute(
                text(
                    "SELECT id, external_id, created_at, title, helpful_count "
                    "FROM reviews"
                )
            ).one()
            assert source == (1, "legacy-dataset", "legacy", None)
            assert product[:6] == (
                11,
                "Legacy title",
                7,
                100000,
                4.0,
                5,
            )
            assert product[6:] == ("[]", 0, "{}")
            assert review[0] == 13
            assert review[1] == "legacy-review"
            assert review[2] is not None
            assert review[3:] == (None, 0)

            connection.execute(text("DELETE FROM shops WHERE id = 7"))
            assert (
                connection.scalar(text("SELECT shop_id FROM products WHERE id = 11"))
                is None
            )
            assert connection.scalar(text("SELECT COUNT(*) FROM products")) == 1
    finally:
        engine.dispose()

    check_database_schema(database_url)


def test_empty_book_metadata_migration_downgrades_and_reapplies(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, "downgrade.db")
    upgrade_database(database_url)

    migration_config = _migration_config()
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            migration_config.attributes["connection"] = connection
            command.downgrade(migration_config, "20260824_0002")
            connection.commit()
        inspector = inspect(engine)
        assert "profile" not in {
            column["name"] for column in inspector.get_columns("dataset_sources")
        }
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20260824_0002"
            )
    finally:
        engine.dispose()

    upgrade_database(database_url)
    check_database_schema(database_url)


def test_postgres_upgrade_downgrade_reapply_preserves_legacy_schema() -> None:
    with disposable_postgres_database() as database_url:
        upgrade_database(database_url, revision="20260830_0003")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO shops (id, name, platform, rating) "
                        "VALUES (991, 'Synthetic legacy shop', 'Tiki', 4.5)"
                    )
                )

            upgrade_database(database_url)
            upgrade_database(database_url)
            check_database_schema(database_url)

            inspector = inspect(engine)
            assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {
                "alembic_version"
            }
            assert (
                str(
                    next(
                        column["type"]
                        for column in inspector.get_columns("v2_knowledge_vectors")
                        if column["name"] == "vector"
                    )
                )
                == "JSONB"
            )
            assert (
                str(
                    next(
                        column["type"]
                        for column in inspector.get_columns("v2_offers")
                        if column["name"] == "demo_price_vnd"
                    )
                )
                == "BIGINT"
            )
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == EXPECTED_DATABASE_REVISION
                )
                assert (
                    connection.scalar(text("SELECT name FROM shops WHERE id = 991"))
                    == "Synthetic legacy shop"
                )

            migration_config = _migration_config()
            with engine.begin() as connection:
                migration_config.attributes["connection"] = connection
                command.downgrade(migration_config, "20260830_0003")

            assert not any(
                table_name.startswith("v2_")
                for table_name in inspect(engine).get_table_names()
            )
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT name FROM shops WHERE id = 991"))
                    == "Synthetic legacy shop"
                )

            upgrade_database(database_url)
            check_database_schema(database_url)
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == EXPECTED_DATABASE_REVISION
                )
                assert (
                    connection.scalar(text("SELECT name FROM shops WHERE id = 991"))
                    == "Synthetic legacy shop"
                )
        finally:
            engine.dispose()


def _database_url(tmp_path: Path, name: str) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / name).as_posix()}"
