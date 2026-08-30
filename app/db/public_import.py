"""Transactional import of a quality-gated public dataset snapshot."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.data.contracts import DatasetManifest, NormalizedProduct, NormalizedReview
from app.data.quality import (
    SnapshotQualityError,
    sha256_file,
    validate_quality_artifacts,
)
from app.db import session as db_session
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review

_DEFAULT_BATCH_SIZE = 1_000
_MAX_BATCH_SIZE = 10_000
RecordT = TypeVar("RecordT", bound=BaseModel)


class PublicImportConflictError(RuntimeError):
    """Raised when a snapshot or existing import fails immutable checks."""


def import_public_snapshot(
    *,
    snapshot_dir: Path,
    session_factory: sessionmaker[Session] | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Validate and stream one prepared snapshot in a single transaction."""

    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= _MAX_BATCH_SIZE
    ):
        raise ValueError(f"batch_size must be between 1 and {_MAX_BATCH_SIZE}")

    snapshot_dir = snapshot_dir.expanduser()
    manifest = _validate_snapshot_artifacts(snapshot_dir)
    source_values = _source_values(snapshot_dir, manifest)
    target_factory = session_factory or db_session.SessionLocal

    try:
        with target_factory.begin() as session:
            existing = session.scalar(
                select(DatasetSource).where(
                    DatasetSource.dataset_id == manifest.dataset_id,
                    DatasetSource.dataset_version == manifest.dataset_version,
                    DatasetSource.profile == manifest.profile,
                )
            )
            if existing is not None:
                _verify_source_facts(existing, source_values)
                return _verified_counts(existing.id, manifest, session)

            source = DatasetSource(**source_values)
            session.add(source)
            session.flush()
            source_id = source.id

            imported_products = _insert_products(
                session,
                source_id=source_id,
                products_path=snapshot_dir / "products.jsonl",
                batch_size=batch_size,
            )
            product_ids = {
                external_id: product_id
                for external_id, product_id in session.execute(
                    select(Product.external_id, Product.id).where(
                        Product.source_id == source_id
                    )
                )
                if external_id is not None
            }
            imported_reviews = _insert_reviews(
                session,
                source_id=source_id,
                reviews_path=snapshot_dir / "reviews.jsonl",
                product_ids=product_ids,
                batch_size=batch_size,
            )
            if (
                imported_products != manifest.product_count
                or imported_reviews != manifest.review_count
            ):
                raise PublicImportConflictError(
                    "streamed row counts do not match the validated manifest"
                )

            revalidated_manifest = _validate_snapshot_artifacts(snapshot_dir)
            if (
                revalidated_manifest != manifest
                or _source_values(
                    snapshot_dir,
                    revalidated_manifest,
                )
                != source_values
            ):
                raise PublicImportConflictError(
                    "snapshot artifacts changed during import"
                )
            return _verified_counts(source_id, manifest, session)
    except IntegrityError as exc:
        raise PublicImportConflictError(
            "database rejected the snapshot as a conflicting import"
        ) from exc


def _validate_snapshot_artifacts(snapshot_dir: Path) -> DatasetManifest:
    try:
        manifest, _ = validate_quality_artifacts(snapshot_dir)
    except SnapshotQualityError as exc:
        raise PublicImportConflictError(
            "snapshot failed quality artifact validation"
        ) from exc
    return manifest


def _source_values(
    snapshot_dir: Path,
    manifest: DatasetManifest,
) -> dict[str, object]:
    try:
        manifest_sha256 = sha256_file(snapshot_dir / "manifest.json")
        quality_report_sha256 = sha256_file(snapshot_dir / "quality-report.json")
    except OSError as exc:
        raise PublicImportConflictError(
            "snapshot artifacts changed after validation"
        ) from exc
    return {
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "profile": manifest.profile,
        "manifest_schema_version": manifest.schema_version,
        "cleaner_version": manifest.cleaner_version,
        "config_version": manifest.config_version,
        "taxonomy_version": manifest.taxonomy_version,
        "source_url": manifest.source_url,
        "source_license": manifest.source_license,
        "source_revision": manifest.source_revision,
        "raw_archive_sha256": manifest.raw_archive_sha256,
        "raw_archive_bytes": manifest.raw_archive_bytes,
        "sampling_policy": manifest.sampling_policy,
        "source_files": list(manifest.source_files),
        "required_product_external_ids": list(manifest.required_product_external_ids),
        "products_sha256": manifest.products_sha256,
        "reviews_sha256": manifest.reviews_sha256,
        "snapshot_sha256": manifest.snapshot_sha256,
        "manifest_sha256": manifest_sha256,
        "quality_report_sha256": quality_report_sha256,
        "sampling_seed": manifest.sampling_seed,
        "product_count": manifest.product_count,
        "review_count": manifest.review_count,
        "retrieved_at": manifest.retrieved_at,
    }


def _verify_source_facts(
    source: DatasetSource,
    expected: dict[str, object],
) -> None:
    mismatches = []
    for field_name, expected_value in expected.items():
        actual_value = getattr(source, field_name)
        if field_name == "retrieved_at":
            actual_value = _naive_utc(actual_value)
            expected_value = _naive_utc(expected_value)
        if actual_value != expected_value:
            mismatches.append(field_name)
    if mismatches:
        raise PublicImportConflictError(
            "dataset identity exists with different immutable provenance"
        )


def _insert_products(
    session: Session,
    *,
    source_id: int,
    products_path: Path,
    batch_size: int,
) -> int:
    imported = 0
    records = _iter_records(products_path, NormalizedProduct)
    for batch in _batches(records, batch_size):
        rows = [
            {
                "source_id": source_id,
                "external_id": product.external_id,
                "shop_id": None,
                "name": product.name,
                "authors": list(product.authors),
                "publisher": product.publisher,
                "category": product.category,
                "page_count": product.page_count,
                "price": product.price_vnd,
                "original_price": product.original_price_vnd,
                "rating": product.rating,
                "sold_count": product.source_popularity,
                "source_review_count": product.source_review_count,
                "cover_url": product.cover_url,
                "description": product.description,
                "platform": product.platform,
                "seller_name": product.seller_name,
                "source_metadata": dict(product.source_metadata),
            }
            for product in batch
        ]
        session.execute(insert(Product), rows)
        imported += len(rows)
    return imported


def _insert_reviews(
    session: Session,
    *,
    source_id: int,
    reviews_path: Path,
    product_ids: dict[str, int],
    batch_size: int,
) -> int:
    imported = 0
    records = _iter_records(reviews_path, NormalizedReview)
    for batch in _batches(records, batch_size):
        rows = []
        for review in batch:
            product_id = product_ids.get(review.product_external_id)
            if product_id is None:
                raise PublicImportConflictError(
                    "validated review references an unavailable product"
                )
            rows.append(
                {
                    "source_id": source_id,
                    "external_id": review.external_id,
                    "product_id": product_id,
                    "rating": review.rating,
                    "title": review.title,
                    "content": review.content,
                    "helpful_count": review.helpful_count,
                    "created_at": review.created_at,
                }
            )
        session.execute(insert(Review), rows)
        imported += len(rows)
    return imported


def _iter_records(path: Path, model: type[RecordT]) -> Iterator[RecordT]:
    try:
        with path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    yield model.model_validate_json(line)
                except ValidationError as exc:
                    raise PublicImportConflictError(
                        f"{path.name} changed after validation at line {line_number}"
                    ) from exc
    except (OSError, UnicodeError) as exc:
        raise PublicImportConflictError(
            f"snapshot artifact became unreadable: {path.name}"
        ) from exc


def _batches(records: Iterable[RecordT], size: int) -> Iterator[list[RecordT]]:
    batch: list[RecordT] = []
    for record in records:
        batch.append(record)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _verified_counts(
    source_id: int,
    manifest: DatasetManifest,
    session: Session,
) -> dict[str, int]:
    product_count = session.scalar(
        select(func.count()).select_from(Product).where(Product.source_id == source_id)
    )
    review_count = session.scalar(
        select(func.count()).select_from(Review).where(Review.source_id == source_id)
    )
    mixed_review_count = session.scalar(
        select(func.count())
        .select_from(Review)
        .join(Product, Review.product_id == Product.id)
        .where(
            Review.source_id == source_id,
            or_(Product.source_id != source_id, Product.source_id.is_(None)),
        )
    )
    counts = {
        "source_id": source_id,
        "products": int(product_count or 0),
        "reviews": int(review_count or 0),
    }
    if (
        counts["products"] != manifest.product_count
        or counts["reviews"] != manifest.review_count
        or mixed_review_count
    ):
        raise PublicImportConflictError(
            "existing import has divergent live rows or mixed provenance"
        )
    return counts


def _naive_utc(value: object) -> object:
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
