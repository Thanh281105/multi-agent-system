"""Download and normalize a small, reproducible Tiki Books snapshot."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.data.contracts import DatasetManifest, NormalizedProduct, NormalizedReview

DEFAULT_SOURCE_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    "biminhc/tiki-books-dataset?datasetVersionNumber=4"
)
DEFAULT_DATASET_VERSION = "kaggle-v4"
DEFAULT_ARCHIVE_SHA256 = (
    "371a618ed66425f8f2738ab01514ab2f20ed1bed645258b509fcce443bd32bbb"
)
DEFAULT_SOURCE_LICENSE = "CC0-1.0"
DEFAULT_SOURCE_REVISION = 4


class DatasetPreparationError(RuntimeError):
    """Raised when a public snapshot cannot be prepared safely."""


class SnapshotExistsError(DatasetPreparationError):
    """Raised when preparation would overwrite an existing snapshot."""


def download_archive(
    *,
    destination: Path,
    url: str = DEFAULT_SOURCE_URL,
    expected_sha256: str | None = DEFAULT_ARCHIVE_SHA256,
    timeout_seconds: int = 120,
) -> Path:
    """Download an archive atomically and verify its checksum."""

    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = expected_sha256.lower() if expected_sha256 else None

    if destination.exists():
        actual = _sha256_file(destination)
        if expected is None or actual == expected:
            return destination
        raise DatasetPreparationError(
            f"existing archive checksum mismatch: expected {expected}, got {actual}"
        )

    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "thanh-v1-data-loader/1.0"},
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)
            temporary.flush()
        except Exception as exc:
            temporary_path.unlink(missing_ok=True)
            raise DatasetPreparationError("dataset download failed") from exc

    actual = _sha256_file(temporary_path)
    if expected is not None and actual != expected:
        temporary_path.unlink(missing_ok=True)
        raise DatasetPreparationError(
            f"download checksum mismatch: expected {expected}, got {actual}"
        )
    temporary_path.replace(destination)
    return destination


def prepare_snapshot(
    *,
    archive_path: Path,
    output_dir: Path,
    target_products: int = 200,
    max_reviews_per_product: int = 10,
    seed: int = 42,
    force: bool = False,
    source_url: str = DEFAULT_SOURCE_URL,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    retrieved_at: datetime | None = None,
) -> DatasetManifest:
    """Prepare a deterministic product/review subset from a Tiki archive."""

    if target_products < 1:
        raise DatasetPreparationError("target_products must be positive")
    if max_reviews_per_product < 1:
        raise DatasetPreparationError("max_reviews_per_product must be positive")

    archive_path = archive_path.expanduser()
    output_dir = output_dir.expanduser()
    if not archive_path.is_file():
        raise DatasetPreparationError(f"archive does not exist: {archive_path}")
    if output_dir.exists() and any(output_dir.iterdir()) and not force:
        raise SnapshotExistsError(
            f"snapshot directory is not empty: {output_dir}; use --force explicitly"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_sha256 = _sha256_file(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        source_files = sorted(Path(name).name for name in archive.namelist())
        books_member = _find_member(archive, "book_data.csv")
        comments_member = _find_member(archive, "comments.csv")
        products, duplicate_rows, skipped_products = _read_products(
            archive,
            books_member,
        )
        selected_products = _select_products(
            products,
            target_count=target_products,
            seed=seed,
        )
        reviews, skipped_reviews = _read_reviews(
            archive,
            comments_member,
            selected_product_ids={product.external_id for product in selected_products},
            max_per_product=max_reviews_per_product,
            seed=seed,
        )

    products_path = output_dir / "products.jsonl"
    reviews_path = output_dir / "reviews.jsonl"
    manifest_path = output_dir / "manifest.json"
    _write_jsonl(
        products_path,
        (product.model_dump(mode="json") for product in selected_products),
    )
    _write_jsonl(reviews_path, (review.model_dump(mode="json") for review in reviews))
    snapshot_sha256 = _sha256_bytes(
        products_path.read_bytes() + b"\n" + reviews_path.read_bytes()
    )
    manifest = DatasetManifest(
        dataset_id="tiki-books",
        dataset_version=dataset_version,
        source_url=source_url,
        source_license=DEFAULT_SOURCE_LICENSE,
        source_revision=DEFAULT_SOURCE_REVISION,
        raw_archive_sha256=raw_sha256,
        raw_archive_bytes=archive_path.stat().st_size,
        retrieved_at=retrieved_at or datetime.now(UTC),
        sampling_seed=seed,
        sampling_policy=(
            "Stable SHA-256 ranking with category round-robin product coverage; "
            f"at most {max_reviews_per_product} reviews per product, "
            "stratified by rating."
        ),
        source_files=source_files,
        duplicate_product_rows=duplicate_rows,
        skipped_product_rows=skipped_products,
        skipped_review_rows=skipped_reviews,
        product_count=len(selected_products),
        review_count=len(reviews),
        snapshot_sha256=snapshot_sha256,
    )
    manifest_path.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _read_products(
    archive: zipfile.ZipFile,
    member: str,
) -> tuple[list[NormalizedProduct], int, int]:
    candidates: dict[str, list[dict[str, str]]] = defaultdict(list)
    skipped = 0
    for row in _csv_rows(archive, member):
        external_id = row.get("product_id", "").strip()
        if not external_id:
            skipped += 1
            continue
        candidates[external_id].append(row)

    normalized: list[NormalizedProduct] = []
    for rows in candidates.values():
        row = max(
            rows,
            key=lambda value: (
                _parse_int(value.get("n_review", "0"), default=0),
                _parse_int(value.get("quantity", "0"), default=0),
                value.get("title", ""),
            ),
        )
        try:
            normalized.append(_normalize_product(row))
        except (TypeError, ValueError):
            skipped += 1
    duplicate_rows = sum(max(len(rows) - 1, 0) for rows in candidates.values())
    normalized.sort(key=lambda product: product.external_id)
    return normalized, duplicate_rows, skipped


def _read_reviews(
    archive: zipfile.ZipFile,
    member: str,
    *,
    selected_product_ids: set[str],
    max_per_product: int,
    seed: int,
) -> tuple[list[NormalizedReview], int]:
    by_product_and_rating: dict[str, dict[int, list[NormalizedReview]]] = defaultdict(
        lambda: defaultdict(list)
    )
    seen_external_ids: dict[str, int] = defaultdict(int)
    skipped = 0
    for row in _csv_rows(archive, member):
        product_id = row.get("product_id", "").strip()
        if product_id not in selected_product_ids:
            continue
        try:
            review = _normalize_review(row, product_id)
        except (TypeError, ValueError):
            skipped += 1
            continue
        occurrence = seen_external_ids[review.external_id]
        seen_external_ids[review.external_id] += 1
        if occurrence:
            review = review.model_copy(
                update={"external_id": f"{review.external_id}:{occurrence}"}
            )
        by_product_and_rating[product_id][review.rating].append(review)

    selected: list[NormalizedReview] = []
    for product_id in sorted(by_product_and_rating):
        rating_groups = by_product_and_rating[product_id]
        for reviews in rating_groups.values():
            reviews.sort(
                key=lambda review: _stable_rank(seed, "review", review.external_id)
            )
        positions = {rating: 0 for rating in range(1, 6)}
        selected_for_product = 0
        while selected_for_product < max_per_product:
            added = False
            for rating in range(1, 6):
                values = rating_groups.get(rating, [])
                position = positions[rating]
                if position < len(values):
                    selected.append(values[position])
                    positions[rating] = position + 1
                    selected_for_product += 1
                    added = True
                    if selected_for_product == max_per_product:
                        break
            if not added:
                break
    return selected, skipped


def _select_products(
    products: list[NormalizedProduct],
    *,
    target_count: int,
    seed: int,
) -> list[NormalizedProduct]:
    eligible = [product for product in products if product.review_count > 0]
    buckets: dict[str, list[NormalizedProduct]] = defaultdict(list)
    for product in eligible:
        buckets[product.category].append(product)
    for values in buckets.values():
        values.sort(
            key=lambda product: _stable_rank(seed, "product", product.external_id)
        )

    selected: list[NormalizedProduct] = []
    categories = sorted(buckets)
    while len(selected) < target_count:
        progress = False
        for category in categories:
            values = buckets[category]
            if values:
                selected.append(values.pop(0))
                progress = True
                if len(selected) == target_count:
                    break
        if not progress:
            break
    selected.sort(key=lambda product: product.external_id)
    return selected


def _normalize_product(row: Mapping[str, str]) -> NormalizedProduct:
    title = _clean_text(row.get("title", ""))
    if not title:
        raise ValueError("product title is required")
    current_price = _parse_int(row.get("current_price", ""))
    original_price = _parse_int(row.get("original_price", ""), default=current_price)
    if current_price < 0 or original_price < 0:
        raise ValueError("product prices must be non-negative")
    rating = _parse_float(row.get("avg_rating", ""))
    if not 0 <= rating <= 5:
        raise ValueError("product rating is outside 0..5")
    source_category = _clean_text(row.get("category", ""))
    category_missing = (
        not source_category or source_category.casefold() == title.casefold()
    )
    category = "Khác" if category_missing else source_category
    author = _clean_text(row.get("authors", ""))
    pages = _parse_int(row.get("pages", ""), default=0)
    manufacturer = _clean_text(row.get("manufacturer", ""))
    detail_parts = [f"Tác giả: {author}" if author else ""]
    detail_parts.append(f"Danh mục: {category}")
    if pages > 0:
        detail_parts.append(f"{pages} trang")
    if manufacturer:
        detail_parts.append(f"Nhà xuất bản: {manufacturer}")
    description = ". ".join(part for part in detail_parts if part)
    return NormalizedProduct(
        external_id=_clean_text(row.get("product_id", "")),
        name=title,
        category=category,
        price_vnd=max(current_price, 0),
        original_price_vnd=max(original_price, current_price, 0),
        rating=rating,
        sold_count=max(_parse_int(row.get("quantity", ""), default=0), 0),
        review_count=max(_parse_int(row.get("n_review", ""), default=0), 0),
        description=description or title,
        seller_name="Người bán không được cung cấp trong Tiki Books Dataset",
        source_metadata={
            "authors": author,
            "category_missing": int(category_missing),
            "source_category": source_category,
            "manufacturer": manufacturer,
            "cover_link": _clean_text(row.get("cover_link", "")),
        },
    )


def _normalize_review(row: Mapping[str, str], product_id: str) -> NormalizedReview:
    content = _clean_text(row.get("content", ""))
    if not content:
        raise ValueError("review content is required")
    comment_id = _clean_text(row.get("comment_id", ""))
    if not comment_id:
        raise ValueError("review comment_id is required")
    rating = _parse_int(row.get("rating", ""))
    if not 1 <= rating <= 5:
        raise ValueError("review rating is outside 1..5")
    return NormalizedReview(
        # The source reuses comment IDs across products; scope them to the
        # product so the normalized source key remains globally unique.
        external_id=f"{product_id}:{comment_id}",
        product_external_id=product_id,
        rating=rating,
        content=content,
        helpful_count=max(_parse_int(row.get("thank_count", ""), default=0), 0),
        source_metadata={"title": _clean_text(row.get("title", ""))},
    )


def _csv_rows(archive: zipfile.ZipFile, member: str) -> Iterator[dict[str, str]]:
    with archive.open(member, "r") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        for row in reader:
            yield {str(key): str(value or "") for key, value in row.items() if key}


def _find_member(archive: zipfile.ZipFile, filename: str) -> str:
    matches = [
        name for name in archive.namelist() if Path(name).name.lower() == filename
    ]
    if len(matches) != 1:
        raise DatasetPreparationError(
            f"expected exactly one {filename} in archive, found {len(matches)}"
        )
    return matches[0]


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            handle.write("\n")


def _stable_rank(seed: int, kind: str, value: str) -> str:
    return hashlib.sha256(f"{seed}:{kind}:{value}".encode("utf-8")).hexdigest()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _parse_int(value: str, *, default: int | None = None) -> int:
    cleaned = _clean_text(value).replace(",", "")
    if not cleaned:
        if default is not None:
            return default
        raise ValueError("integer value is empty")
    try:
        return int(cleaned)
    except ValueError:
        try:
            return int(float(cleaned))
        except ValueError:
            if default is not None:
                return default
            raise


def _parse_float(value: str) -> float:
    cleaned = _clean_text(value).replace(",", "")
    return float(cleaned)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
