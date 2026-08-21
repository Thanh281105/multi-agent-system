"""Product ORM model."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utc_now

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
            "platform IN ('Shopee', 'Tiki', 'Lazada')",
            name="ck_products_platform",
        ),
        Index("ix_products_category_price_rating", "category", "price", "rating"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(220), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    original_price: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    sold_count: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
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

    shop: Mapped["Shop"] = relationship(back_populates="products")
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )
