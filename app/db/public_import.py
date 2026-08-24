"""Transactional import of a prepared public dataset snapshot."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.data.contracts import DatasetManifest, NormalizedProduct, NormalizedReview
from app.db import session as db_session
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop

PUBLIC_SOURCE_SHOP_NAME = "Tiki Books Dataset (seller không có trong nguồn)"


class PublicImportConflictError(RuntimeError):
    """Raised when a dataset identity already exists with different facts."""


def import_public_snapshot(
    *,
    snapshot_dir: Path,
    session_factory: sessionmaker[Session] | None = None,
) -> dict[str, int]:
    """Import a validated snapshot once and return stable counts."""

    snapshot_dir = snapshot_dir.expanduser()
    manifest = _load_manifest(snapshot_dir / "manifest.json")
    products = _load_products(snapshot_dir / "products.jsonl")
    reviews = _load_reviews(snapshot_dir / "reviews.jsonl")
    _validate_snapshot(manifest, products, reviews)
    actual_snapshot_sha256 = _snapshot_sha256(snapshot_dir)
    if actual_snapshot_sha256 != manifest.snapshot_sha256.lower():
        raise PublicImportConflictError("snapshot checksum does not match manifest")
    target_factory = session_factory or db_session.SessionLocal

    with target_factory.begin() as session:
        existing = session.scalar(
            select(DatasetSource).where(
                DatasetSource.dataset_id == manifest.dataset_id,
                DatasetSource.dataset_version == manifest.dataset_version,
            )
        )
        if existing is not None:
            if existing.snapshot_sha256 != manifest.snapshot_sha256:
                raise PublicImportConflictError(
                    "dataset identity exists with a different snapshot checksum"
                )
            if existing.product_count != len(products) or existing.review_count != len(
                reviews
            ):
                raise PublicImportConflictError(
                    "dataset identity exists with different row counts"
                )
            return _counts(existing.id, session)

        source = DatasetSource(
            dataset_id=manifest.dataset_id,
            dataset_version=manifest.dataset_version,
            source_url=manifest.source_url,
            source_license=manifest.source_license,
            source_revision=manifest.source_revision,
            raw_archive_sha256=manifest.raw_archive_sha256,
            snapshot_sha256=manifest.snapshot_sha256,
            sampling_seed=manifest.sampling_seed,
            product_count=len(products),
            review_count=len(reviews),
            retrieved_at=manifest.retrieved_at,
        )
        session.add(source)
        session.flush()

        shop = session.scalar(
            select(Shop).where(
                Shop.name == PUBLIC_SOURCE_SHOP_NAME,
                Shop.platform == "Tiki",
            )
        )
        if shop is None:
            shop = Shop(
                name=PUBLIC_SOURCE_SHOP_NAME,
                platform="Tiki",
                rating=0.0,
                created_at=_BASE_TIME,
            )
            session.add(shop)
            session.flush()

        product_rows = [
            Product(
                source_id=source.id,
                external_id=product.external_id,
                shop_id=shop.id,
                name=product.name,
                category=product.category,
                price=product.price_vnd,
                original_price=product.original_price_vnd,
                rating=product.rating,
                sold_count=product.sold_count,
                description=product.description,
                platform=product.platform,
                created_at=_BASE_TIME + timedelta(minutes=index),
                updated_at=_BASE_TIME + timedelta(minutes=index),
            )
            for index, product in enumerate(products)
        ]
        session.add_all(product_rows)
        session.flush()
        product_ids = {product.external_id: product.id for product in product_rows}
        session.add_all(
            [
                Review(
                    source_id=source.id,
                    external_id=review.external_id,
                    product_id=product_ids[review.product_external_id],
                    rating=review.rating,
                    content=review.content,
                    created_at=_BASE_TIME + timedelta(days=1, minutes=index),
                )
                for index, review in enumerate(reviews)
            ]
        )

    return {
        "source_id": source.id,
        "products": len(products),
        "reviews": len(reviews),
    }


_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _load_manifest(path: Path) -> DatasetManifest:
    if not path.is_file():
        raise PublicImportConflictError(f"manifest does not exist: {path}")
    return DatasetManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _load_products(path: Path) -> list[NormalizedProduct]:
    return [
        NormalizedProduct.model_validate(json.loads(line)) for line in _read_lines(path)
    ]


def _load_reviews(path: Path) -> list[NormalizedReview]:
    return [
        NormalizedReview.model_validate(json.loads(line)) for line in _read_lines(path)
    ]


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        raise PublicImportConflictError(f"snapshot file does not exist: {path}")
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _validate_snapshot(
    manifest: DatasetManifest,
    products: list[NormalizedProduct],
    reviews: list[NormalizedReview],
) -> None:
    if len(products) != manifest.product_count or len(reviews) != manifest.review_count:
        raise PublicImportConflictError(
            "manifest row counts do not match snapshot files"
        )
    product_ids = [product.external_id for product in products]
    review_ids = [review.external_id for review in reviews]
    if len(product_ids) != len(set(product_ids)):
        raise PublicImportConflictError(
            "snapshot contains duplicate product external IDs"
        )
    if len(review_ids) != len(set(review_ids)):
        raise PublicImportConflictError(
            "snapshot contains duplicate review external IDs"
        )
    product_id_set = set(product_ids)
    if any(review.product_external_id not in product_id_set for review in reviews):
        raise PublicImportConflictError("snapshot contains an orphan review")


def _snapshot_sha256(snapshot_dir: Path) -> str:
    digest = hashlib.sha256()
    digest.update((snapshot_dir / "products.jsonl").read_bytes())
    digest.update(b"\n")
    digest.update((snapshot_dir / "reviews.jsonl").read_bytes())
    return digest.hexdigest()


def _counts(source_id: int, session: Session) -> dict[str, int]:
    product_count = (
        session.query(Product).filter(Product.source_id == source_id).count()
    )
    review_count = session.query(Review).filter(Review.source_id == source_id).count()
    return {
        "source_id": source_id,
        "products": product_count,
        "reviews": review_count,
    }
