"""Compatibility bootstrap for one configured, quality-gated snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.data.contracts import DatasetManifest
from app.data.quality import SnapshotQualityError, validate_quality_artifacts
from app.db import session as db_session
from app.db.public_import import PublicImportConflictError, import_public_snapshot
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop


class SeedConflictError(RuntimeError):
    """Raised when snapshot bootstrap would create or preserve mixed data."""


def seed_database(
    *,
    snapshot_dir: Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> dict[str, int]:
    """Verify/import only the configured public snapshot, idempotently."""

    configured_snapshot = (snapshot_dir or settings.public_snapshot_dir).expanduser()
    target_factory = session_factory or db_session.SessionLocal
    try:
        manifest, _ = validate_quality_artifacts(configured_snapshot)
        _verify_bootstrap_state(target_factory, manifest)
        imported = import_public_snapshot(
            snapshot_dir=configured_snapshot,
            session_factory=target_factory,
        )
        _verify_bootstrap_state(target_factory, manifest)
    except (PublicImportConflictError, SnapshotQualityError) as exc:
        raise SeedConflictError(
            "configured snapshot bootstrap failed its integrity checks"
        ) from exc
    return imported


def _verify_bootstrap_state(
    session_factory: sessionmaker[Session],
    manifest: DatasetManifest,
) -> None:
    with session_factory() as session:
        sources = list(
            session.scalars(select(DatasetSource).order_by(DatasetSource.id))
        )
        shops = _count(session, Shop)
        products = _count(session, Product)
        reviews = _count(session, Review)
        if not sources:
            if shops or products or reviews:
                raise SeedConflictError(
                    "database contains unprovenanced or legacy rows; bootstrap refused"
                )
            return

        if len(sources) != 1 or shops:
            raise SeedConflictError(
                "database contains mixed or unknown data; bootstrap refused"
            )
        source = sources[0]
        if (
            source.dataset_id != manifest.dataset_id
            or source.dataset_version != manifest.dataset_version
            or source.profile != manifest.profile
        ):
            raise SeedConflictError(
                "database contains a different snapshot; bootstrap refused"
            )

        scoped_products = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.source_id == source.id)
        )
        scoped_reviews = session.scalar(
            select(func.count())
            .select_from(Review)
            .where(Review.source_id == source.id)
        )
        mixed_reviews = session.scalar(
            select(func.count())
            .select_from(Review)
            .outerjoin(Product, Review.product_id == Product.id)
            .where(
                or_(
                    Product.id.is_(None),
                    Product.source_id.is_(None),
                    Review.source_id.is_(None),
                    Product.source_id != Review.source_id,
                )
            )
        )
        if (
            products != manifest.product_count
            or reviews != manifest.review_count
            or int(scoped_products or 0) != products
            or int(scoped_reviews or 0) != reviews
            or mixed_reviews
        ):
            raise SeedConflictError(
                "database snapshot rows have mixed or divergent provenance"
            )


def _count(
    session: Session, model: type[DatasetSource | Shop | Product | Review]
) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify/import the configured quality-gated book snapshot.",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="override PUBLIC_SNAPSHOT_DIR for this bootstrap run",
    )
    arguments = parser.parse_args()
    try:
        counts = seed_database(snapshot_dir=arguments.snapshot)
    except SeedConflictError as exc:
        parser.error(str(exc))
    print("Tiki Books snapshot verified")
    print(f"Source: {counts['source_id']}")
    print(f"Products: {counts['products']}")
    print(f"Reviews: {counts['reviews']}")


if __name__ == "__main__":
    main()
