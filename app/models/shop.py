"""Shop ORM model."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utc_now

if TYPE_CHECKING:
    from app.models.product import Product


class Shop(Base):
    """Marketplace shop that owns products."""

    __tablename__ = "shops"
    __table_args__ = (
        CheckConstraint(
            "rating >= 0 AND rating <= 5",
            name="ck_shops_rating_range",
        ),
        CheckConstraint(
            "platform IN ('Shopee', 'Tiki', 'Lazada')",
            name="ck_shops_platform",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="shop",
    )
