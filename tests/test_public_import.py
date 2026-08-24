"""Tests for public snapshot integrity and idempotent database import."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.data.contracts import DatasetManifest, NormalizedProduct, NormalizedReview
from app.db.base import Base
from app.db.public_import import (
    PublicImportConflictError,
    import_public_snapshot,
)
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review


def test_public_snapshot_import_is_idempotent_and_provenanced(tmp_path: Path) -> None:
    engine, session_factory = _isolated_database(tmp_path)
    snapshot_dir = tmp_path / "snapshot"
    _write_snapshot(snapshot_dir)

    try:
        first = import_public_snapshot(
            snapshot_dir=snapshot_dir,
            session_factory=session_factory,
        )
        second = import_public_snapshot(
            snapshot_dir=snapshot_dir,
            session_factory=session_factory,
        )

        assert first == second == {"source_id": 1, "products": 2, "reviews": 3}
        with session_factory() as session:
            source = session.scalar(select(DatasetSource))
            assert source is not None
            assert source.dataset_id == "fixture-books"
            assert session.scalar(select(Product).where(Product.source_id == source.id))
            assert session.scalar(select(Review).where(Review.source_id == source.id))
            assert (
                session.scalar(
                    select(Product).where(
                        Product.external_id == "book-1",
                        Product.source_id == source.id,
                    )
                )
                is not None
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_public_snapshot_import_rejects_changed_dataset_identity(
    tmp_path: Path,
) -> None:
    engine, session_factory = _isolated_database(tmp_path)
    first_dir = tmp_path / "first"
    changed_dir = tmp_path / "changed"
    _write_snapshot(first_dir, product_name="Original title")
    _write_snapshot(changed_dir, product_name="Changed title")

    try:
        import_public_snapshot(
            snapshot_dir=first_dir,
            session_factory=session_factory,
        )
        with pytest.raises(PublicImportConflictError, match="different snapshot"):
            import_public_snapshot(
                snapshot_dir=changed_dir,
                session_factory=session_factory,
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _isolated_database(
    tmp_path: Path,
) -> tuple[object, sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'import.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def _write_snapshot(snapshot_dir: Path, *, product_name: str = "Fixture title") -> None:
    snapshot_dir.mkdir(parents=True)
    products = [
        NormalizedProduct(
            external_id="book-1",
            name=product_name,
            category="Văn học",
            price_vnd=120_000,
            original_price_vnd=150_000,
            rating=4.5,
            sold_count=12,
            review_count=2,
            description="Mô tả fixture",
            seller_name="Fixture seller",
        ),
        NormalizedProduct(
            external_id="book-2",
            name="Second title",
            category="Kinh tế",
            price_vnd=90_000,
            original_price_vnd=90_000,
            rating=4.0,
            sold_count=5,
            review_count=1,
            description="Mô tả thứ hai",
            seller_name="Fixture seller",
        ),
    ]
    reviews = [
        NormalizedReview(
            external_id="review-1",
            product_external_id="book-1",
            rating=5,
            content="Rất tốt",
        ),
        NormalizedReview(
            external_id="review-2",
            product_external_id="book-1",
            rating=4,
            content="Đáng đọc",
        ),
        NormalizedReview(
            external_id="review-3",
            product_external_id="book-2",
            rating=3,
            content="Ổn",
        ),
    ]
    products_bytes = _write_jsonl(
        snapshot_dir / "products.jsonl",
        [product.model_dump(mode="json") for product in products],
    )
    reviews_bytes = _write_jsonl(
        snapshot_dir / "reviews.jsonl",
        [review.model_dump(mode="json") for review in reviews],
    )
    snapshot_sha256 = hashlib.sha256(products_bytes + b"\n" + reviews_bytes).hexdigest()
    manifest = DatasetManifest(
        dataset_id="fixture-books",
        dataset_version="v1",
        source_url="https://example.test/fixture-books",
        source_license="CC0-1.0",
        source_revision=1,
        raw_archive_sha256="a" * 64,
        raw_archive_bytes=1,
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        sampling_seed=42,
        sampling_policy="Two products and three reviews for import tests.",
        source_files=["books.csv", "comments.csv"],
        duplicate_product_rows=0,
        skipped_product_rows=0,
        skipped_review_rows=0,
        product_count=len(products),
        review_count=len(reviews),
        snapshot_sha256=snapshot_sha256,
    )
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> bytes:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for row in rows
    ).encode("utf-8")
    path.write_bytes(payload)
    return payload
