"""Quality-gate construction and artifact integrity validation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.data.cleaning import CleaningStats, contains_pii
from app.data.contracts import (
    DataQualityReport,
    DatasetManifest,
    NormalizedProduct,
    NormalizedReview,
)


class SnapshotQualityError(RuntimeError):
    """A prepared snapshot is missing or fails its PII-free quality contract."""


def build_quality_report(
    *,
    manifest: DatasetManifest,
    products: Sequence[NormalizedProduct],
    reviews: Sequence[NormalizedReview],
    stats: CleaningStats,
    output_counts: Mapping[str, int],
    snapshot_dir: Path,
) -> DataQualityReport:
    """Evaluate deterministic invariants without recording source row content."""

    products_path = snapshot_dir / "products.jsonl"
    reviews_path = snapshot_dir / "reviews.jsonl"
    manifest_path = snapshot_dir / "manifest.json"
    products_hash = sha256_file(products_path)
    reviews_hash = sha256_file(reviews_path)
    combined_hash = snapshot_sha256(products_path, reviews_path)
    output_hashes = {
        "manifest.json": sha256_file(manifest_path),
        "products.jsonl": products_hash,
        "reviews.jsonl": reviews_hash,
        "snapshot": combined_hash,
    }

    failures: list[str] = []
    product_ids = [product.external_id for product in products]
    review_ids = [review.external_id for review in reviews]
    if len(product_ids) != len(set(product_ids)):
        failures.append("duplicate_product_external_id")
    if len(review_ids) != len(set(review_ids)):
        failures.append("duplicate_review_external_id")
    product_id_set = set(product_ids)
    if any(review.product_external_id not in product_id_set for review in reviews):
        failures.append("orphan_review")
    if any(product.platform != "Tiki" for product in products):
        failures.append("non_tiki_listing")
    if any(
        contains_pii(review.content) or contains_pii(review.title or "")
        for review in reviews
    ):
        failures.append("review_pii_pattern")
    required_ids = set(manifest.required_product_external_ids)
    if not required_ids.issubset(product_id_set):
        failures.append("missing_required_product_external_id")
    if len(products) != manifest.product_count:
        failures.append("manifest_product_count_mismatch")
    if len(reviews) != manifest.review_count:
        failures.append("manifest_review_count_mismatch")
    expected_reviews = output_counts.get("profile_target_reviews")
    if expected_reviews is not None and len(reviews) != expected_reviews:
        failures.append("profile_review_target_mismatch")
    if products_hash != manifest.products_sha256:
        failures.append("manifest_products_hash_mismatch")
    if reviews_hash != manifest.reviews_sha256:
        failures.append("manifest_reviews_hash_mismatch")
    if combined_hash != manifest.snapshot_sha256:
        failures.append("manifest_snapshot_hash_mismatch")

    return DataQualityReport(
        status="fail" if failures else "pass",
        profile=manifest.profile,
        cleaner_version=manifest.cleaner_version,
        config_version=manifest.config_version,
        taxonomy_version=manifest.taxonomy_version,
        raw_archive_sha256=manifest.raw_archive_sha256,
        input_counts=_sorted_counts(stats.input_counts),
        output_counts=dict(sorted(output_counts.items())),
        correction_counts=_sorted_counts(stats.correction_counts),
        drop_counts=_sorted_counts(stats.drop_counts),
        redaction_counts=_sorted_counts(stats.redaction_counts),
        field_coverage=_field_coverage(products, reviews),
        output_hashes=output_hashes,
        gate_failures=sorted(failures),
    )


def validate_quality_artifacts(
    snapshot_dir: Path,
) -> tuple[DatasetManifest, DataQualityReport]:
    """Fail closed unless all four snapshot artifacts agree byte-for-byte."""

    snapshot_dir = snapshot_dir.expanduser()
    manifest_path = snapshot_dir / "manifest.json"
    quality_path = snapshot_dir / "quality-report.json"
    products_path = snapshot_dir / "products.jsonl"
    reviews_path = snapshot_dir / "reviews.jsonl"
    for path in (manifest_path, quality_path, products_path, reviews_path):
        if not path.is_file():
            raise SnapshotQualityError(
                f"required snapshot artifact is missing: {path.name}"
            )
    try:
        manifest = DatasetManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        report = DataQualityReport.model_validate_json(
            quality_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SnapshotQualityError("snapshot metadata is invalid") from exc
    if report.status != "pass" or report.gate_failures:
        raise SnapshotQualityError("snapshot quality report did not pass")
    if (
        report.profile != manifest.profile
        or report.cleaner_version != manifest.cleaner_version
        or report.config_version != manifest.config_version
        or report.taxonomy_version != manifest.taxonomy_version
        or report.raw_archive_sha256 != manifest.raw_archive_sha256
    ):
        raise SnapshotQualityError("quality report provenance does not match manifest")

    actual = {
        "manifest.json": sha256_file(manifest_path),
        "products.jsonl": sha256_file(products_path),
        "reviews.jsonl": sha256_file(reviews_path),
        "snapshot": snapshot_sha256(products_path, reviews_path),
    }
    if report.output_hashes != actual:
        raise SnapshotQualityError("quality report artifact hashes do not match")
    if actual["products.jsonl"] != manifest.products_sha256:
        raise SnapshotQualityError("products hash does not match manifest")
    if actual["reviews.jsonl"] != manifest.reviews_sha256:
        raise SnapshotQualityError("reviews hash does not match manifest")
    if actual["snapshot"] != manifest.snapshot_sha256:
        raise SnapshotQualityError("snapshot hash does not match manifest")
    if _count_jsonl(products_path) != manifest.product_count:
        raise SnapshotQualityError("product count does not match manifest")
    if _count_jsonl(reviews_path) != manifest.review_count:
        raise SnapshotQualityError("review count does not match manifest")
    if report.output_counts.get("products") != manifest.product_count:
        raise SnapshotQualityError("quality report product count does not match")
    if report.output_counts.get("reviews") != manifest.review_count:
        raise SnapshotQualityError("quality report review count does not match")
    _validate_normalized_records(products_path, reviews_path, manifest)
    return manifest, report


def sha256_file(path: Path) -> str:
    """Hash one file incrementally."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_sha256(products_path: Path, reviews_path: Path) -> str:
    """Hash the ordered JSONL pair without loading full snapshots in memory."""

    digest = hashlib.sha256()
    for path in (products_path, reviews_path):
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if path == products_path:
            digest.update(b"\n")
    return digest.hexdigest()


def _field_coverage(
    products: Sequence[NormalizedProduct],
    reviews: Sequence[NormalizedReview],
) -> dict[str, float]:
    product_fields = {
        "product.authors": lambda item: bool(item.authors),
        "product.publisher": lambda item: item.publisher is not None,
        "product.page_count": lambda item: item.page_count is not None,
        "product.cover_url": lambda item: item.cover_url is not None,
        "product.original_price": lambda item: item.original_price_vnd is not None,
        "product.rating": lambda item: item.rating is not None,
        "product.source_popularity": lambda item: item.source_popularity is not None,
        "product.seller": lambda item: item.seller_name is not None,
    }
    review_fields = {
        "review.title": lambda item: item.title is not None,
        "review.helpful_count": lambda item: item.helpful_count is not None,
        "review.source_timestamp": lambda item: item.created_at is not None,
    }
    coverage = {
        name: _coverage(products, predicate)
        for name, predicate in product_fields.items()
    }
    coverage.update(
        {
            name: _coverage(reviews, predicate)
            for name, predicate in review_fields.items()
        }
    )
    return dict(sorted(coverage.items()))


def _coverage(
    values: Sequence[Any],
    predicate: Callable[[Any], bool],
) -> float:
    if not values:
        return 0.0
    matched = sum(1 for value in values if predicate(value))
    return round(matched / len(values), 6)


def _sorted_counts(values: Mapping[str, int]) -> dict[str, int]:
    return {key: values[key] for key in sorted(values) if values[key]}


def _count_jsonl(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except (OSError, UnicodeError) as exc:
        raise SnapshotQualityError(
            f"snapshot artifact is unreadable: {path.name}"
        ) from exc


def _validate_normalized_records(
    products_path: Path,
    reviews_path: Path,
    manifest: DatasetManifest,
) -> None:
    product_ids: set[str] = set()
    review_ids: set[str] = set()
    try:
        with products_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                product = NormalizedProduct.model_validate_json(line)
                if product.external_id in product_ids:
                    raise SnapshotQualityError(
                        "snapshot contains duplicate product external IDs"
                    )
                product_ids.add(product.external_id)
        required_ids = set(manifest.required_product_external_ids)
        if not required_ids.issubset(product_ids):
            raise SnapshotQualityError(
                "snapshot is missing required product external IDs"
            )
        with reviews_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                review = NormalizedReview.model_validate_json(line)
                if review.external_id in review_ids:
                    raise SnapshotQualityError(
                        "snapshot contains duplicate review external IDs"
                    )
                if review.product_external_id not in product_ids:
                    raise SnapshotQualityError("snapshot contains an orphan review")
                if contains_pii(review.content) or contains_pii(review.title or ""):
                    raise SnapshotQualityError(
                        "snapshot contains a normalized review PII pattern"
                    )
                review_ids.add(review.external_id)
    except SnapshotQualityError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise SnapshotQualityError(
            "snapshot contains an invalid normalized record"
        ) from exc
