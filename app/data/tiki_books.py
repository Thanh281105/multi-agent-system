"""Pinned Tiki Books download and deterministic clean-snapshot preparation."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.data.cleaning import (
    CLEANER_VERSION,
    TAXONOMY_VERSION,
    CleaningStats,
    clean_products,
    clean_reviews,
    select_profile_products,
    select_profile_reviews,
)
from app.data.contracts import DataProfile, DatasetManifest
from app.data.profiles import CONFIG_VERSION, DEFAULT_SAMPLING_SEED, get_profile_config
from app.data.quality import (
    SnapshotQualityError,
    build_quality_report,
    sha256_file,
    snapshot_sha256,
    validate_quality_artifacts,
)

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
DEFAULT_RETRIEVED_AT = datetime(2026, 8, 24, 3, 47, 30, 42_026, tzinfo=UTC)

_BOOK_HEADERS = (
    "product_id",
    "title",
    "authors",
    "original_price",
    "current_price",
    "quantity",
    "category",
    "n_review",
    "avg_rating",
    "pages",
    "manufacturer",
    "cover_link",
)
_REVIEW_HEADERS = (
    "product_id",
    "comment_id",
    "title",
    "thank_count",
    "customer_id",
    "rating",
    "content",
)
_REQUIRED_MEMBERS = {
    "book_data.csv": _BOOK_HEADERS,
    "comments.csv": _REVIEW_HEADERS,
}
_ARTIFACT_NAMES = {
    "products.jsonl",
    "reviews.jsonl",
    "manifest.json",
    "quality-report.json",
}


class DatasetPreparationError(RuntimeError):
    """Raised when a public snapshot cannot be prepared safely."""


class SnapshotExistsError(DatasetPreparationError):
    """Raised when preparation would overwrite an existing snapshot."""


def download_archive(
    *,
    destination: Path,
    url: str = DEFAULT_SOURCE_URL,
    expected_sha256: str = DEFAULT_ARCHIVE_SHA256,
    timeout_seconds: int = 120,
) -> Path:
    """Download an archive atomically and verify its pinned checksum."""

    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = _validated_sha256(expected_sha256)
    if destination.exists():
        actual = sha256_file(destination)
        if actual == expected:
            return destination
        raise DatasetPreparationError(
            f"existing archive checksum mismatch: expected {expected}, got {actual}"
        )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "tiki-books-data-loader/1.0"},
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)
            temporary.flush()
        actual = sha256_file(temporary_path)
        if actual != expected:
            raise DatasetPreparationError(
                f"download checksum mismatch: expected {expected}, got {actual}"
            )
        temporary_path.replace(destination)
    except DatasetPreparationError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise DatasetPreparationError("dataset download failed") from exc
    return destination


def prepare_snapshot(
    *,
    archive_path: Path,
    output_dir: Path,
    profile: DataProfile,
    force: bool = False,
    expected_sha256: str = DEFAULT_ARCHIVE_SHA256,
    source_url: str = DEFAULT_SOURCE_URL,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    retrieved_at: datetime = DEFAULT_RETRIEVED_AT,
    seed: int = DEFAULT_SAMPLING_SEED,
) -> DatasetManifest:
    """Run verify -> clean -> redact -> normalize -> gate -> profile."""

    archive_path = archive_path.expanduser()
    output_dir = output_dir.expanduser()
    if not archive_path.is_file():
        raise DatasetPreparationError(f"archive does not exist: {archive_path}")
    expected = _validated_sha256(expected_sha256)
    raw_sha256 = sha256_file(archive_path)
    if raw_sha256 != expected:
        raise DatasetPreparationError(
            f"archive checksum mismatch: expected {expected}, got {raw_sha256}"
        )
    _prepare_output_directory(output_dir, force=force)

    stats = CleaningStats()
    config = get_profile_config(profile)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validate_archive(archive)
            products = clean_products(
                _csv_rows(archive, members["book_data.csv"]),
                stats,
            )
            product_ids = {product.external_id for product in products}
            reviews = clean_reviews(
                _csv_rows(archive, members["comments.csv"]),
                product_external_ids=product_ids,
                stats=stats,
            )
    except (csv.Error, UnicodeError, zipfile.BadZipFile) as exc:
        raise DatasetPreparationError("archive CSV data is invalid") from exc

    selected_products = select_profile_products(
        products,
        reviews,
        config=config,
        seed=seed,
    )
    selected_product_ids = {product.external_id for product in selected_products}
    selected_reviews = select_profile_reviews(
        reviews,
        selected_product_ids=selected_product_ids,
        config=config,
        seed=seed,
    )
    _write_jsonl(
        output_dir / "products.jsonl",
        (product.model_dump(mode="json") for product in selected_products),
    )
    _write_jsonl(
        output_dir / "reviews.jsonl",
        (review.model_dump(mode="json") for review in selected_reviews),
    )

    products_path = output_dir / "products.jsonl"
    reviews_path = output_dir / "reviews.jsonl"
    manifest = DatasetManifest(
        dataset_version=dataset_version,
        profile=profile,
        cleaner_version=CLEANER_VERSION,
        config_version=CONFIG_VERSION,
        taxonomy_version=TAXONOMY_VERSION,
        source_url=source_url,
        source_license=DEFAULT_SOURCE_LICENSE,
        source_revision=DEFAULT_SOURCE_REVISION,
        raw_archive_sha256=raw_sha256,
        raw_archive_bytes=archive_path.stat().st_size,
        retrieved_at=retrieved_at,
        sampling_seed=seed,
        sampling_policy=config.sampling_policy,
        source_files=sorted(_REQUIRED_MEMBERS),
        required_product_external_ids=list(config.required_product_external_ids),
        product_count=len(selected_products),
        review_count=len(selected_reviews),
        products_sha256=sha256_file(products_path),
        reviews_sha256=sha256_file(reviews_path),
        snapshot_sha256=snapshot_sha256(products_path, reviews_path),
    )
    _write_json(output_dir / "manifest.json", manifest.model_dump(mode="json"))
    report = build_quality_report(
        manifest=manifest,
        products=selected_products,
        reviews=selected_reviews,
        stats=stats,
        output_counts={
            "cleaned_products": len(products),
            "cleaned_reviews": len(reviews),
            "products": len(selected_products),
            "reviews": len(selected_reviews),
            **(
                {"profile_target_reviews": config.target_reviews}
                if config.target_reviews is not None
                else {}
            ),
        },
        snapshot_dir=output_dir,
    )
    _write_json(output_dir / "quality-report.json", report.model_dump(mode="json"))
    if report.status != "pass":
        failure_codes = ",".join(report.gate_failures)
        raise DatasetPreparationError(f"snapshot quality gate failed: {failure_codes}")
    try:
        validate_quality_artifacts(output_dir)
    except SnapshotQualityError as exc:
        raise DatasetPreparationError("snapshot artifact validation failed") from exc
    return manifest


def _prepare_output_directory(output_dir: Path, *, force: bool) -> None:
    if output_dir.exists():
        entries = {path.name for path in output_dir.iterdir()}
        if entries and not force:
            raise SnapshotExistsError(
                f"snapshot directory is not empty: {output_dir}; use --force explicitly"
            )
        unrelated = entries - _ARTIFACT_NAMES
        if unrelated:
            raise SnapshotExistsError(
                "snapshot directory contains unrelated files and will not be "
                "overwritten"
            )
        for name in entries:
            path = output_dir / name
            if not path.is_file():
                raise SnapshotExistsError(
                    "snapshot directory contains a non-file artifact"
                )
        for name in entries:
            path = output_dir / name
            path.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)


def _validate_archive(archive: zipfile.ZipFile) -> dict[str, str]:
    members: dict[str, str] = {}
    for filename, expected_headers in _REQUIRED_MEMBERS.items():
        member = _find_member(archive, filename)
        try:
            with archive.open(member, "r") as raw:
                header_line = raw.readline().decode("utf-8-sig", errors="strict")
                headers = tuple(next(csv.reader([header_line])))
        except (StopIteration, UnicodeError, csv.Error) as exc:
            raise DatasetPreparationError(
                f"required CSV header is invalid: {filename}"
            ) from exc
        if headers != expected_headers:
            raise DatasetPreparationError(f"required CSV schema mismatch: {filename}")
        members[filename] = member
    return members


def _csv_rows(
    archive: zipfile.ZipFile,
    member: str,
) -> Iterator[dict[str, str]]:
    with archive.open(member, "r") as raw:
        text = io.TextIOWrapper(
            raw,
            encoding="utf-8-sig",
            errors="strict",
            newline="",
        )
        reader = csv.DictReader(text)
        for row in reader:
            yield {
                str(key): str(value or "")
                for key, value in row.items()
                if key is not None
            }


def _find_member(archive: zipfile.ZipFile, filename: str) -> str:
    matches = [
        name
        for name in archive.namelist()
        if Path(name).name.casefold() == filename.casefold()
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
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _validated_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise DatasetPreparationError("expected SHA-256 must be 64 hexadecimal digits")
    return normalized
