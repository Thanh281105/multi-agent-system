"""Tests for deterministic public dataset normalization."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.data.tiki_books import (
    DatasetPreparationError,
    SnapshotExistsError,
    prepare_snapshot,
)


def _make_archive(path: Path) -> None:
    books = [
        [
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
        ],
        [
            "1",
            "Sách Một",
            "Tác giả A",
            "100000",
            "80000",
            "20",
            "Tiểu thuyết",
            "5",
            "4.5",
            "100",
            "NXB A",
            "",
        ],
        [
            "1",
            "Sách Một",
            "Tác giả A",
            "100000",
            "80000",
            "20",
            "Tiểu thuyết",
            "5",
            "4.5",
            "100",
            "NXB A",
            "",
        ],
        [
            "2",
            "Sách Hai",
            "Tác giả B",
            "200000",
            "150000",
            "30",
            "Kỹ năng",
            "3",
            "4.0",
            "200",
            "NXB B",
            "",
        ],
        [
            "3",
            "",
            "Tác giả C",
            "100000",
            "90000",
            "1",
            "Khác",
            "1",
            "4.0",
            "80",
            "NXB C",
            "",
        ],
    ]
    comments = [
        [
            "product_id",
            "comment_id",
            "title",
            "thank_count",
            "customer_id",
            "rating",
            "content",
        ],
        ["1", "101", "Tốt", "2", "private-user", "5", "Đóng gói rất kỹ."],
        ["1", "102", "Ổn", "1", "private-user", "3", "Nội dung dễ đọc."],
        ["1", "103", "", "0", "private-user", "9", "Sai rating sẽ bị bỏ."],
        ["2", "201", "Hay", "0", "private-user", "4", "Giá hợp lý."],
        ["9", "901", "", "0", "private-user", "5", "Không thuộc sản phẩm chọn."],
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rows in (("book_data.csv", books), ("comments.csv", comments)):
            archive.writestr(name, "\n".join(",".join(row) for row in rows) + "\n")


def test_prepare_snapshot_is_deterministic_and_drops_invalid_rows(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.zip"
    _make_archive(archive)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_manifest = prepare_snapshot(
        archive_path=archive,
        output_dir=first,
        target_products=2,
        max_reviews_per_product=2,
        seed=42,
        retrieved_at=None,
    )
    second_manifest = prepare_snapshot(
        archive_path=archive,
        output_dir=second,
        target_products=2,
        max_reviews_per_product=2,
        seed=42,
        retrieved_at=first_manifest.retrieved_at,
    )

    assert first_manifest.product_count == 2
    assert first_manifest.review_count == 3
    assert first_manifest.duplicate_product_rows == 1
    assert first_manifest.skipped_product_rows == 1
    assert first_manifest.skipped_review_rows == 1
    assert first_manifest.snapshot_sha256 == second_manifest.snapshot_sha256
    assert (first / "products.jsonl").read_bytes() == (
        second / "products.jsonl"
    ).read_bytes()
    assert (first / "reviews.jsonl").read_bytes() == (
        second / "reviews.jsonl"
    ).read_bytes()
    products = [
        json.loads(line)
        for line in (first / "products.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    reviews = [
        json.loads(line)
        for line in (first / "reviews.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {item["external_id"] for item in products} == {"1", "2"}
    assert {item["external_id"] for item in reviews} == {
        "1:101",
        "1:102",
        "2:201",
    }
    assert len({item["external_id"] for item in reviews}) == len(reviews)
    assert all("customer_id" not in item for item in reviews)


def test_prepare_refuses_non_empty_output_without_force(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    _make_archive(archive)
    output = tmp_path / "snapshot"
    output.mkdir()
    (output / "do-not-overwrite.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(SnapshotExistsError):
        prepare_snapshot(archive_path=archive, output_dir=output)


def test_prepare_requires_a_source_archive(tmp_path: Path) -> None:
    with pytest.raises(DatasetPreparationError, match="archive does not exist"):
        prepare_snapshot(
            archive_path=tmp_path / "missing.zip",
            output_dir=tmp_path / "snapshot",
        )
