"""Add immutable dataset provenance and external source identifiers.

Revision ID: 20260824_0002
Revises: 20260824_0001
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0002"
down_revision: str | None = "20260824_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.String(length=100), nullable=False),
        sa.Column("dataset_version", sa.String(length=100), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("source_license", sa.String(length=200), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("raw_archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("sampling_seed", sa.Integer(), nullable=False),
        sa.Column("product_count", sa.Integer(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id",
            "dataset_version",
            name="uq_dataset_sources_identity",
        ),
    )
    op.create_index("ix_dataset_sources_dataset_id", "dataset_sources", ["dataset_id"])

    with op.batch_alter_table("products", recreate="always") as batch:
        batch.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("external_id", sa.String(length=128), nullable=True))
        batch.create_foreign_key(
            "fk_products_source_id_dataset_sources",
            "dataset_sources",
            ["source_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_products_source_id", "products", ["source_id"])
    op.create_index(
        "ux_products_source_external_id",
        "products",
        ["source_id", "external_id"],
        unique=True,
    )

    with op.batch_alter_table("reviews", recreate="always") as batch:
        batch.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("external_id", sa.String(length=128), nullable=True))
        batch.create_foreign_key(
            "fk_reviews_source_id_dataset_sources",
            "dataset_sources",
            ["source_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_reviews_source_id", "reviews", ["source_id"])
    op.create_index(
        "ux_reviews_source_external_id",
        "reviews",
        ["source_id", "external_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_reviews_source_external_id", table_name="reviews")
    op.drop_index("ix_reviews_source_id", table_name="reviews")
    with op.batch_alter_table("reviews", recreate="always") as batch:
        batch.drop_constraint(
            "fk_reviews_source_id_dataset_sources",
            type_="foreignkey",
        )
        batch.drop_column("external_id")
        batch.drop_column("source_id")

    op.drop_index("ux_products_source_external_id", table_name="products")
    op.drop_index("ix_products_source_id", table_name="products")
    with op.batch_alter_table("products", recreate="always") as batch:
        batch.drop_constraint(
            "fk_products_source_id_dataset_sources",
            type_="foreignkey",
        )
        batch.drop_column("external_id")
        batch.drop_column("source_id")

    op.drop_index("ix_dataset_sources_dataset_id", table_name="dataset_sources")
    op.drop_table("dataset_sources")
