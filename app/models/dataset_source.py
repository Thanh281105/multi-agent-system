"""Dataset provenance model for imported public snapshots."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utc_now

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.review import Review


class DatasetSource(Base):
    """Immutable identity and checksums for one imported dataset snapshot."""

    __tablename__ = "dataset_sources"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "dataset_version",
            name="uq_dataset_sources_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    source_license: Mapped[str] = mapped_column(String(200), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sampling_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    product_count: Mapped[int] = mapped_column(Integer, nullable=False)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="dataset_source",
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="dataset_source",
    )
