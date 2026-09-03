"""Persist normalized Tiki book metadata and profile-scoped provenance.

Revision ID: 20260830_0003
Revises: 20260824_0002
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0003"
down_revision: str | None = "20260824_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQLITE_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def upgrade() -> None:
    _upgrade_dataset_sources()
    _upgrade_products()
    _upgrade_reviews()


def downgrade() -> None:
    _assert_downgrade_compatible()
    _downgrade_reviews()
    _downgrade_products()
    _downgrade_dataset_sources()


def _upgrade_dataset_sources() -> None:
    columns = (
        sa.Column("profile", sa.String(length=32), nullable=True),
        sa.Column("manifest_schema_version", sa.String(length=16), nullable=True),
        sa.Column("cleaner_version", sa.String(length=100), nullable=True),
        sa.Column("config_version", sa.String(length=100), nullable=True),
        sa.Column("taxonomy_version", sa.String(length=100), nullable=True),
        sa.Column("raw_archive_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sampling_policy", sa.String(length=500), nullable=True),
        sa.Column("source_files", sa.JSON(), nullable=True),
        sa.Column("required_product_external_ids", sa.JSON(), nullable=True),
        sa.Column("products_sha256", sa.String(length=64), nullable=True),
        sa.Column("reviews_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("quality_report_sha256", sa.String(length=64), nullable=True),
    )
    if _is_sqlite():
        with op.batch_alter_table("dataset_sources", recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
            batch.drop_constraint("uq_dataset_sources_identity", type_="unique")
        op.execute(
            sa.text(
                "UPDATE dataset_sources SET profile = 'legacy' WHERE profile IS NULL"
            )
        )
        with op.batch_alter_table("dataset_sources", recreate="always") as batch:
            batch.alter_column(
                "profile",
                existing_type=sa.String(length=32),
                nullable=False,
            )
            batch.create_check_constraint(
                "ck_dataset_sources_profile",
                "profile IN ('legacy', 'test', 'eval', 'full')",
            )
            batch.create_unique_constraint(
                "uq_dataset_sources_identity_profile",
                ["dataset_id", "dataset_version", "profile"],
            )
        return

    for column in columns:
        op.add_column("dataset_sources", column)
    op.execute(
        sa.text("UPDATE dataset_sources SET profile = 'legacy' WHERE profile IS NULL")
    )
    op.drop_constraint(
        "uq_dataset_sources_identity",
        "dataset_sources",
        type_="unique",
    )
    op.alter_column(
        "dataset_sources",
        "profile",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_dataset_sources_profile",
        "dataset_sources",
        "profile IN ('legacy', 'test', 'eval', 'full')",
    )
    op.create_unique_constraint(
        "uq_dataset_sources_identity_profile",
        "dataset_sources",
        ["dataset_id", "dataset_version", "profile"],
    )


def _upgrade_products() -> None:
    new_columns = (
        sa.Column(
            "authors",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("publisher", sa.String(length=220), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "source_review_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("cover_url", sa.String(length=2000), nullable=True),
        sa.Column("seller_name", sa.String(length=160), nullable=True),
        sa.Column(
            "source_metadata",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    if _is_sqlite():
        with op.batch_alter_table(
            "products",
            recreate="always",
            naming_convention=_SQLITE_NAMING_CONVENTION,
        ) as batch:
            batch.drop_constraint(
                "fk_products_shop_id_shops",
                type_="foreignkey",
            )
            batch.alter_column(
                "shop_id",
                existing_type=sa.Integer(),
                nullable=True,
            )
            batch.alter_column(
                "category",
                existing_type=sa.String(length=80),
                type_=sa.String(length=120),
                existing_nullable=False,
            )
            for column_name, column_type in (
                ("original_price", sa.Integer()),
                ("rating", sa.Float()),
                ("sold_count", sa.Integer()),
            ):
                batch.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=True,
                )
            for column in new_columns:
                batch.add_column(column)
            batch.create_check_constraint(
                "ck_products_page_count_range",
                "page_count IS NULL OR (page_count >= 1 AND page_count <= 20000)",
            )
            batch.create_check_constraint(
                "ck_products_source_review_count_non_negative",
                "source_review_count >= 0",
            )
            batch.create_foreign_key(
                "fk_products_shop_id_shops",
                "shops",
                ["shop_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return

    shop_foreign_key = _foreign_key_name("products", ("shop_id",))
    op.drop_constraint(shop_foreign_key, "products", type_="foreignkey")
    op.alter_column(
        "products",
        "shop_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        "products",
        "category",
        existing_type=sa.String(length=80),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
    for column_name, column_type in (
        ("original_price", sa.Integer()),
        ("rating", sa.Float()),
        ("sold_count", sa.Integer()),
    ):
        op.alter_column(
            "products",
            column_name,
            existing_type=column_type,
            nullable=True,
        )
    for column in new_columns:
        op.add_column("products", column)
    op.create_check_constraint(
        "ck_products_page_count_range",
        "products",
        "page_count IS NULL OR (page_count >= 1 AND page_count <= 20000)",
    )
    op.create_check_constraint(
        "ck_products_source_review_count_non_negative",
        "products",
        "source_review_count >= 0",
    )
    op.create_foreign_key(
        "fk_products_shop_id_shops",
        "products",
        "shops",
        ["shop_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _upgrade_reviews() -> None:
    new_columns = (
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "helpful_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    if _is_sqlite():
        with op.batch_alter_table("reviews", recreate="always") as batch:
            batch.alter_column(
                "external_id",
                existing_type=sa.String(length=128),
                type_=sa.String(length=257),
                existing_nullable=True,
            )
            batch.alter_column(
                "created_at",
                existing_type=sa.DateTime(),
                nullable=True,
                server_default=None,
            )
            for column in new_columns:
                batch.add_column(column)
            batch.create_check_constraint(
                "ck_reviews_helpful_count_non_negative",
                "helpful_count >= 0",
            )
        return

    op.alter_column(
        "reviews",
        "external_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=257),
        existing_nullable=True,
    )
    op.alter_column(
        "reviews",
        "created_at",
        existing_type=sa.DateTime(),
        nullable=True,
        server_default=None,
    )
    for column in new_columns:
        op.add_column("reviews", column)
    op.create_check_constraint(
        "ck_reviews_helpful_count_non_negative",
        "reviews",
        "helpful_count >= 0",
    )


def _downgrade_reviews() -> None:
    if _is_sqlite():
        with op.batch_alter_table("reviews", recreate="always") as batch:
            batch.drop_constraint(
                "ck_reviews_helpful_count_non_negative",
                type_="check",
            )
            batch.drop_column("helpful_count")
            batch.drop_column("title")
            batch.alter_column(
                "created_at",
                existing_type=sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
            batch.alter_column(
                "external_id",
                existing_type=sa.String(length=257),
                type_=sa.String(length=128),
                existing_nullable=True,
            )
        return

    op.drop_constraint(
        "ck_reviews_helpful_count_non_negative",
        "reviews",
        type_="check",
    )
    for column_name in ("helpful_count", "title"):
        op.drop_column("reviews", column_name)
    op.alter_column(
        "reviews",
        "created_at",
        existing_type=sa.DateTime(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    op.alter_column(
        "reviews",
        "external_id",
        existing_type=sa.String(length=257),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def _downgrade_products() -> None:
    columns_to_drop = (
        "source_metadata",
        "seller_name",
        "cover_url",
        "source_review_count",
        "page_count",
        "publisher",
        "authors",
    )
    if _is_sqlite():
        with op.batch_alter_table(
            "products",
            recreate="always",
            naming_convention=_SQLITE_NAMING_CONVENTION,
        ) as batch:
            batch.drop_constraint(
                "ck_products_source_review_count_non_negative",
                type_="check",
            )
            batch.drop_constraint("ck_products_page_count_range", type_="check")
            batch.drop_constraint(
                "fk_products_shop_id_shops",
                type_="foreignkey",
            )
            for column_name in columns_to_drop:
                batch.drop_column(column_name)
            for column_name, column_type in (
                ("original_price", sa.Integer()),
                ("rating", sa.Float()),
                ("sold_count", sa.Integer()),
            ):
                batch.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=False,
                )
            batch.alter_column(
                "category",
                existing_type=sa.String(length=120),
                type_=sa.String(length=80),
                existing_nullable=False,
            )
            batch.alter_column(
                "shop_id",
                existing_type=sa.Integer(),
                nullable=False,
            )
            batch.create_foreign_key(
                "fk_products_shop_id_shops",
                "shops",
                ["shop_id"],
                ["id"],
                ondelete="CASCADE",
            )
        return

    op.drop_constraint(
        "ck_products_source_review_count_non_negative",
        "products",
        type_="check",
    )
    op.drop_constraint("ck_products_page_count_range", "products", type_="check")
    shop_foreign_key = _foreign_key_name("products", ("shop_id",))
    op.drop_constraint(shop_foreign_key, "products", type_="foreignkey")
    for column_name in columns_to_drop:
        op.drop_column("products", column_name)
    for column_name, column_type in (
        ("original_price", sa.Integer()),
        ("rating", sa.Float()),
        ("sold_count", sa.Integer()),
    ):
        op.alter_column(
            "products",
            column_name,
            existing_type=column_type,
            nullable=False,
        )
    op.alter_column(
        "products",
        "category",
        existing_type=sa.String(length=120),
        type_=sa.String(length=80),
        existing_nullable=False,
    )
    op.alter_column(
        "products",
        "shop_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_products_shop_id_shops",
        "products",
        "shops",
        ["shop_id"],
        ["id"],
        ondelete="CASCADE",
    )


def _downgrade_dataset_sources() -> None:
    columns_to_drop = (
        "quality_report_sha256",
        "manifest_sha256",
        "reviews_sha256",
        "products_sha256",
        "required_product_external_ids",
        "source_files",
        "sampling_policy",
        "raw_archive_bytes",
        "taxonomy_version",
        "config_version",
        "cleaner_version",
        "manifest_schema_version",
        "profile",
    )
    if _is_sqlite():
        with op.batch_alter_table("dataset_sources", recreate="always") as batch:
            batch.drop_constraint(
                "uq_dataset_sources_identity_profile",
                type_="unique",
            )
            batch.drop_constraint("ck_dataset_sources_profile", type_="check")
            for column_name in columns_to_drop:
                batch.drop_column(column_name)
            batch.create_unique_constraint(
                "uq_dataset_sources_identity",
                ["dataset_id", "dataset_version"],
            )
        return

    op.drop_constraint(
        "uq_dataset_sources_identity_profile",
        "dataset_sources",
        type_="unique",
    )
    op.drop_constraint(
        "ck_dataset_sources_profile",
        "dataset_sources",
        type_="check",
    )
    for column_name in columns_to_drop:
        op.drop_column("dataset_sources", column_name)
    op.create_unique_constraint(
        "uq_dataset_sources_identity",
        "dataset_sources",
        ["dataset_id", "dataset_version"],
    )


def _assert_downgrade_compatible() -> None:
    connection = op.get_bind()
    incompatible_products = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM products "
            "WHERE shop_id IS NULL OR original_price IS NULL "
            "OR rating IS NULL OR sold_count IS NULL OR length(category) > 80"
        )
    )
    incompatible_reviews = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM reviews "
            "WHERE created_at IS NULL OR length(external_id) > 128"
        )
    )
    duplicate_source_identities = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "SELECT dataset_id, dataset_version FROM dataset_sources "
            "GROUP BY dataset_id, dataset_version HAVING COUNT(*) > 1"
            ") AS duplicate_sources"
        )
    )
    if incompatible_products or incompatible_reviews or duplicate_source_identities:
        raise RuntimeError(
            "cannot downgrade book metadata without fabricating or deleting facts"
        )


def _foreign_key_name(table_name: str, columns: tuple[str, ...]) -> str:
    inspector = sa.inspect(op.get_bind())
    for foreign_key in inspector.get_foreign_keys(table_name):
        if tuple(foreign_key["constrained_columns"]) == columns:
            name = foreign_key.get("name")
            if isinstance(name, str) and name:
                return name
    raise RuntimeError(f"named foreign key is unavailable: {table_name}{columns}")


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"
