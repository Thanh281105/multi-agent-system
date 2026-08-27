"""Create the initial structured e-commerce schema.

Revision ID: 20260824_0001
Revises: None
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shops",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("rating", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "platform IN ('Shopee', 'Tiki', 'Lazada')",
            name="ck_shops_platform",
        ),
        sa.CheckConstraint(
            "rating >= 0 AND rating <= 5",
            name="ck_shops_rating_range",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shops_name", "shops", ["name"])
    op.create_index("ix_shops_platform", "shops", ["platform"])

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=220), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("original_price", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Float(), nullable=False),
        sa.Column("sold_count", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "original_price >= 0",
            name="ck_products_original_price_non_negative",
        ),
        sa.CheckConstraint(
            "platform IN ('Shopee', 'Tiki', 'Lazada')",
            name="ck_products_platform",
        ),
        sa.CheckConstraint(
            "price >= 0",
            name="ck_products_price_non_negative",
        ),
        sa.CheckConstraint(
            "rating >= 0 AND rating <= 5",
            name="ck_products_rating_range",
        ),
        sa.CheckConstraint(
            "sold_count >= 0",
            name="ck_products_sold_count_non_negative",
        ),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_products_category", "products", ["category"])
    op.create_index(
        "ix_products_category_price_rating",
        "products",
        ["category", "price", "rating"],
    )
    op.create_index("ix_products_name", "products", ["name"])
    op.create_index("ix_products_platform", "products", ["platform"])
    op.create_index("ix_products_rating", "products", ["rating"])
    op.create_index("ix_products_shop_id", "products", ["shop_id"])
    op.create_index("ix_products_sold_count", "products", ["sold_count"])

    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rating >= 1 AND rating <= 5",
            name="ck_reviews_rating_range",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reviews_product_id", "reviews", ["product_id"])
    op.create_index(
        "ix_reviews_product_created_at",
        "reviews",
        ["product_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("reviews")
    op.drop_table("products")
    op.drop_table("shops")
