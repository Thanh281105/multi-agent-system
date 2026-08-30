"""Product ORM model."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utc_now
from app.models.dataset_source import DatasetSource

if TYPE_CHECKING:
    from app.models.review import Review
    from app.models.shop import Shop


class Product(Base):
    """Product listed by a shop on a marketplace platform."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_products_price_non_negative"),
        CheckConstraint(
            "original_price >= 0",
            name="ck_products_original_price_non_negative",
        ),
        CheckConstraint(
            "rating >= 0 AND rating <= 5",
            name="ck_products_rating_range",
        ),
        CheckConstraint(
            "sold_count >= 0",
            name="ck_products_sold_count_non_negative",
        ),
        CheckConstraint(
            "page_count IS NULL OR (page_count >= 1 AND page_count <= 20000)",
            name="ck_products_page_count_range",
        ),
        CheckConstraint(
            "source_review_count >= 0",
            name="ck_products_source_review_count_non_negative",
        ),
        CheckConstraint(
            "platform IN ('Shopee', 'Tiki', 'Lazada')",
            name="ck_products_platform",
        ),
        Index("ix_products_category_price_rating", "category", "price", "rating"),
        Index(
            "ux_products_source_external_id",
            "source_id",
            "external_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("dataset_sources.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(220), nullable=False, index=True)
    authors: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    publisher: Mapped[str | None] = mapped_column(String(220), nullable=True)
    category: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    original_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    sold_count: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_review_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    cover_url: Mapped[str | None] = mapped_column(String(2_000), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    seller_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_metadata: Mapped[dict[str, str | int | float | bool | None]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    shop: Mapped["Shop | None"] = relationship(back_populates="products")
    dataset_source: Mapped["DatasetSource | None"] = relationship(
        DatasetSource,
        back_populates="products",
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )
