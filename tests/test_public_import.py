"""Tests for quality-gated, streaming public snapshot import."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.data.contracts import (
    DataQualityReport,
    DatasetManifest,
    NormalizedProduct,
    NormalizedReview,
)
from app.data.quality import sha256_file, snapshot_sha256
from app.db.base import Base
from app.db.public_import import (
    PublicImportConflictError,
    import_public_snapshot,
)
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop


def test_public_snapshot_import_streams_metadata_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_factory = _isolated_database(tmp_path)
    snapshot_dir = tmp_path / "snapshot"
    _write_snapshot(snapshot_dir)
    original_read_text = Path.read_text

    def reject_jsonl_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.suffix == ".jsonl":
            raise AssertionError("JSONL import must stream instead of read_text")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_jsonl_read_text)

    try:
        first = import_public_snapshot(
            snapshot_dir=snapshot_dir,
            session_factory=session_factory,
            batch_size=1,
        )
        second = import_public_snapshot(
            snapshot_dir=snapshot_dir,
            session_factory=session_factory,
            batch_size=1,
        )

        assert first == second == {"source_id": 1, "products": 2, "reviews": 3}
        with session_factory() as session:
            source = session.scalar(select(DatasetSource))
            assert source is not None
            assert source.dataset_id == "tiki-books"
            assert source.dataset_version == "fixture-v1"
            assert source.profile == "test"
            assert source.cleaner_version == "fixture-cleaner-1"
            assert source.config_version == "fixture-config-1"
            assert source.taxonomy_version == "fixture-taxonomy-1"
            assert source.manifest_sha256 == sha256_file(snapshot_dir / "manifest.json")
            assert source.quality_report_sha256 == sha256_file(
                snapshot_dir / "quality-report.json"
            )
            assert session.scalar(select(func.count(Shop.id))) == 0

            first_book = session.scalar(
                select(Product).where(
                    Product.external_id == "book-1",
                    Product.source_id == source.id,
                )
            )
            assert first_book is not None
            assert first_book.shop_id is None
            assert first_book.seller_name == "Nhà sách nguồn"
            assert first_book.authors == ["Tác giả A", "Tác giả B"]
            assert first_book.publisher == "NXB Fixture"
            assert first_book.page_count == 320
            assert first_book.cover_url == "https://salt.tikicdn.com/fixture.jpg"
            assert first_book.source_review_count == 2
            assert first_book.sold_count == 12
            assert first_book.source_metadata == {
                "category_was_mapped": True,
                "source_category": "Sach van hoc",
            }

            second_book = session.scalar(
                select(Product).where(Product.external_id == "book-2")
            )
            assert second_book is not None
            assert second_book.seller_name is None
            assert second_book.original_price is None
            assert second_book.rating is None
            assert second_book.sold_count is None

            reviews = list(
                session.scalars(
                    select(Review)
                    .where(Review.source_id == source.id)
                    .order_by(Review.id)
                )
            )
            assert reviews[0].title == "Rất hay"
            assert reviews[0].helpful_count == 7
            assert reviews[0].created_at is None
            assert reviews[1].created_at == datetime(2025, 5, 1, 8, 30)
            assert len(reviews[1].external_id or "") == 257
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("failure", ["missing", "failed", "tampered"])
def test_public_import_rejects_invalid_quality_before_database_writes(
    tmp_path: Path,
    failure: str,
) -> None:
    engine, session_factory = _isolated_database(tmp_path)
    snapshot_dir = tmp_path / "snapshot"
    _write_snapshot(snapshot_dir)
    quality_path = snapshot_dir / "quality-report.json"
    if failure == "missing":
        quality_path.unlink()
    elif failure == "failed":
        report = json.loads(quality_path.read_text(encoding="utf-8"))
        report["status"] = "fail"
        report["gate_failures"] = ["fixture_failure"]
        _write_json(quality_path, report)
    else:
        products_path = snapshot_dir / "products.jsonl"
        products_path.write_bytes(products_path.read_bytes() + b" ")

    try:
        with pytest.raises(PublicImportConflictError, match="quality artifact"):
            import_public_snapshot(
                snapshot_dir=snapshot_dir,
                session_factory=session_factory,
            )
        with session_factory() as session:
            assert session.scalar(select(func.count(DatasetSource.id))) == 0
            assert session.scalar(select(func.count(Product.id))) == 0
            assert session.scalar(select(func.count(Review.id))) == 0
            assert session.scalar(select(func.count(Shop.id))) == 0
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_public_import_rejects_changed_identity_and_divergent_live_rows(
    tmp_path: Path,
) -> None:
    engine, session_factory = _isolated_database(tmp_path)
    original_dir = tmp_path / "original"
    changed_dir = tmp_path / "changed"
    _write_snapshot(original_dir)
    _write_snapshot(changed_dir, product_name="Changed title")

    try:
        imported = import_public_snapshot(
            snapshot_dir=original_dir,
            session_factory=session_factory,
        )
        with pytest.raises(PublicImportConflictError, match="immutable provenance"):
            import_public_snapshot(
                snapshot_dir=changed_dir,
                session_factory=session_factory,
            )

        with session_factory.begin() as session:
            session.execute(
                delete(Review).where(Review.source_id == imported["source_id"])
            )
        with pytest.raises(PublicImportConflictError, match="divergent live rows"):
            import_public_snapshot(
                snapshot_dir=original_dir,
                session_factory=session_factory,
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_profiles_share_dataset_version_without_source_collision(
    tmp_path: Path,
) -> None:
    engine, session_factory = _isolated_database(tmp_path)
    test_dir = tmp_path / "test"
    eval_dir = tmp_path / "eval"
    _write_snapshot(test_dir, profile="test")
    _write_snapshot(eval_dir, profile="eval")

    try:
        test_counts = import_public_snapshot(
            snapshot_dir=test_dir,
            session_factory=session_factory,
        )
        eval_counts = import_public_snapshot(
            snapshot_dir=eval_dir,
            session_factory=session_factory,
        )
        assert test_counts["source_id"] != eval_counts["source_id"]
        with session_factory() as session:
            sources = list(
                session.scalars(select(DatasetSource).order_by(DatasetSource.id))
            )
            assert [source.profile for source in sources] == ["test", "eval"]
            assert session.scalar(select(func.count(Product.id))) == 4
            assert session.scalar(select(func.count(Review.id))) == 6
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _isolated_database(
    tmp_path: Path,
) -> tuple[Engine, sessionmaker[Session]]:
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


def _write_snapshot(
    snapshot_dir: Path,
    *,
    product_name: str = "Fixture title",
    profile: str = "test",
) -> None:
    snapshot_dir.mkdir(parents=True)
    products = [
        NormalizedProduct(
            external_id="book-1",
            name=product_name,
            authors=["Tác giả A", "Tác giả B"],
            publisher="NXB Fixture",
            category="Văn học",
            page_count=320,
            price_vnd=120_000,
            original_price_vnd=150_000,
            rating=4.5,
            source_popularity=12,
            source_review_count=2,
            cover_url="https://salt.tikicdn.com/fixture.jpg",
            description="Mô tả fixture",
            seller_name="Nhà sách nguồn",
            source_metadata={
                "source_category": "Sach van hoc",
                "category_was_mapped": True,
            },
        ),
        NormalizedProduct(
            external_id="book-2",
            name="Second title",
            authors=[],
            publisher=None,
            category="Kinh tế",
            page_count=None,
            price_vnd=90_000,
            original_price_vnd=None,
            rating=None,
            source_popularity=None,
            source_review_count=1,
            cover_url=None,
            description="Mô tả thứ hai",
            seller_name=None,
        ),
    ]
    long_review_id = "book-1:" + "r" * 250
    reviews = [
        NormalizedReview(
            external_id="book-1:review-1",
            product_external_id="book-1",
            rating=5,
            title="Rất hay",
            content="Đáng đọc.",
            helpful_count=7,
            created_at=None,
        ),
        NormalizedReview(
            external_id=long_review_id,
            product_external_id="book-1",
            rating=4,
            content="Nội dung tốt.",
            created_at=datetime(2025, 5, 1, 8, 30),
        ),
        NormalizedReview(
            external_id="book-2:review-3",
            product_external_id="book-2",
            rating=3,
            content="Ổn.",
        ),
    ]
    products_path = snapshot_dir / "products.jsonl"
    reviews_path = snapshot_dir / "reviews.jsonl"
    _write_jsonl(
        products_path,
        [product.model_dump(mode="json") for product in products],
    )
    _write_jsonl(
        reviews_path,
        [review.model_dump(mode="json") for review in reviews],
    )
    manifest = DatasetManifest(
        dataset_version="fixture-v1",
        profile=profile,  # type: ignore[arg-type]
        cleaner_version="fixture-cleaner-1",
        config_version="fixture-config-1",
        taxonomy_version="fixture-taxonomy-1",
        source_url="https://example.test/fixture-books",
        source_license="CC0-1.0",
        source_revision=1,
        raw_archive_sha256="a" * 64,
        raw_archive_bytes=1,
        retrieved_at=datetime(2026, 1, 1),
        sampling_seed=42,
        sampling_policy="Two products and three reviews for import tests.",
        source_files=["books.csv", "comments.csv"],
        required_product_external_ids=["book-1", "book-2"],
        product_count=len(products),
        review_count=len(reviews),
        products_sha256=sha256_file(products_path),
        reviews_sha256=sha256_file(reviews_path),
        snapshot_sha256=snapshot_sha256(products_path, reviews_path),
    )
    manifest_path = snapshot_dir / "manifest.json"
    _write_json(manifest_path, manifest.model_dump(mode="json"))
    report = DataQualityReport(
        status="pass",
        profile=manifest.profile,
        cleaner_version=manifest.cleaner_version,
        config_version=manifest.config_version,
        taxonomy_version=manifest.taxonomy_version,
        raw_archive_sha256=manifest.raw_archive_sha256,
        input_counts={"book_rows": 2, "review_rows": 3},
        output_counts={"products": 2, "reviews": 3},
        correction_counts={},
        drop_counts={},
        redaction_counts={},
        field_coverage={},
        output_hashes={
            "manifest.json": sha256_file(manifest_path),
            "products.jsonl": manifest.products_sha256,
            "reviews.jsonl": manifest.reviews_sha256,
            "snapshot": manifest.snapshot_sha256,
        },
    )
    _write_json(
        snapshot_dir / "quality-report.json",
        report.model_dump(mode="json"),
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
