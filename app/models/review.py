"""Product review ORM model."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.dataset_source import DatasetSource

if TYPE_CHECKING:
    from app.models.product import Product


class Review(Base):
    """Vietnamese customer review attached to one product."""

    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
        CheckConstraint(
            "helpful_count >= 0",
            name="ck_reviews_helpful_count_non_negative",
        ),
        Index("ix_reviews_product_created_at", "product_id", "created_at"),
        Index(
            "ux_reviews_source_external_id",
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
        String(257),
        nullable=True,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    helpful_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime | None] = mapped_column(nullable=True)

    product: Mapped["Product"] = relationship(back_populates="reviews")
    dataset_source: Mapped["DatasetSource | None"] = relationship(
        DatasetSource,
        back_populates="reviews",
    )
