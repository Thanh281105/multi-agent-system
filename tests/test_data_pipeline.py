"""Tests for deterministic, privacy-preserving Tiki Books preparation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.data.profiles import get_profile_config
from app.data.quality import (
    SnapshotQualityError,
    sha256_file,
    snapshot_sha256,
    validate_quality_artifacts,
)
from app.data.tiki_books import (
    DatasetPreparationError,
    SnapshotExistsError,
    download_archive,
    prepare_snapshot,
)

BOOK_HEADERS = [
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
]
REVIEW_HEADERS = [
    "product_id",
    "comment_id",
    "title",
    "thank_count",
    "customer_id",
    "rating",
    "content",
]


def test_profiles_pin_stable_required_book_ids() -> None:
    test = get_profile_config("test")
    evaluation = get_profile_config("eval")
    full = get_profile_config("full")

    assert len(test.required_product_external_ids) == test.target_products == 24
    assert len(evaluation.required_product_external_ids) == 24
    assert evaluation.target_products == 200
    assert evaluation.target_reviews == 1_773
    assert set(test.required_product_external_ids).issubset(
        evaluation.required_product_external_ids
    )
    assert full.required_product_external_ids == ()


def test_prepare_is_reproducible_redacts_pii_and_preserves_review_signal(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.zip"
    books = [
        [
            "1",
            "Sa\u0301ch &amp; Một\u0000",
            "Tác giả A; Tác giả A",
            "100000",
            "80000",
            "20",
            "Sa\u0301ch &amp; Một",
            "5",
            "4.5",
            "100",
            "NXB A",
            "https://salt.tikicdn.com/cache/cover.jpg#fragment",
        ],
        [
            "1",
            "Sách thiếu dữ liệu",
            "",
            "",
            "80000",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "2",
            "Sách Hai",
            "Tác giả B",
            "200000",
            "150000",
            "30",
            "Kiến Thức Bách Khoa",
            "3",
            "4.0",
            "200",
            "NXB B",
            "http://untrusted.example/cover.jpg",
        ],
    ]
    reviews = [
        [
            "1",
            "101",
            "Liên hệ test@example.com",
            "2",
            "private-user",
            "5",
            "Gọi 090 123 4567 hoặc xem https://example.test/path",
        ],
        ["1", "101", "", "0", "another-user", "5", "Bản thiếu thông tin"],
        ["1", "102", "", "0", "private-user", "1", "Tệ."],
        ["1", "103", "", "0", "private-user", "1", "Tệ."],
        ["2", "201", "Ổn", "0", "private-user", "4", "Giá hợp lý."],
        ["9", "901", "", "0", "private-user", "5", "Review orphan."],
        ["2", "202", "", "0", "private-user", "9", "Sai rating."],
        ["2", "203", "", "0", "private-user", "3", "  \u0000  "],
    ]
    _make_archive(archive, books=books, reviews=reviews)

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = _prepare(archive, first, profile="full")
    second_manifest = _prepare(archive, second, profile="full")

    assert first_manifest == second_manifest
    for name in (
        "products.jsonl",
        "reviews.jsonl",
        "manifest.json",
        "quality-report.json",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes()

    products = _read_jsonl(first / "products.jsonl")
    normalized_reviews = _read_jsonl(first / "reviews.jsonl")
    report = json.loads((first / "quality-report.json").read_text(encoding="utf-8"))
    assert products[0]["name"] == "Sách & Một"
    assert products[0]["authors"] == ["Tác giả A"]
    assert products[0]["category"] == "Khác"
    assert products[0]["cover_url"] == ("https://salt.tikicdn.com/cache/cover.jpg")
    assert products[0]["seller_name"] is None
    assert products[1]["category"] == "Kiến thức - Bách khoa"
    assert products[1]["cover_url"] is None
    assert {review["external_id"] for review in normalized_reviews} == {
        "1:101",
        "1:102",
        "1:103",
        "2:201",
    }
    assert sum(review["content"] == "Tệ." for review in normalized_reviews) == 2
    redacted = next(
        review for review in normalized_reviews if review["external_id"] == "1:101"
    )
    assert redacted["title"] == "Liên hệ [EMAIL]"
    assert redacted["content"] == "Gọi [PHONE] hoặc xem [URL]"
    assert redacted["created_at"] is None
    assert all("customer_id" not in review for review in normalized_reviews)
    assert report["status"] == "pass"
    assert report["redaction_counts"] == {"email": 1, "phone": 1, "url": 1}
    assert report["drop_counts"]["review_duplicate_identity"] == 1
    assert report["drop_counts"]["review_orphan"] == 1
    assert report["drop_counts"]["review_invalid_rating"] == 1
    assert report["drop_counts"]["review_empty_content"] == 1
    assert report["correction_counts"]["product_duplicate_identity"] == 1
    validate_quality_artifacts(first)


def test_conflict_dedupe_is_independent_of_csv_row_order(tmp_path: Path) -> None:
    product_a = [
        "1",
        "Alpha",
        "Author A",
        "100000",
        "90000",
        "1",
        "Tiểu Thuyết",
        "2",
        "4",
        "100",
        "Publisher",
        "",
    ]
    product_b = [
        "1",
        "Beta",
        "Author B",
        "100000",
        "90000",
        "1",
        "Tiểu Thuyết",
        "2",
        "4",
        "100",
        "Publisher",
        "",
    ]
    review_a = ["1", "10", "A", "1", "private", "5", "Nội dung A"]
    review_b = ["1", "10", "B", "1", "private", "5", "Nội dung B"]
    first_archive = tmp_path / "first.zip"
    second_archive = tmp_path / "second.zip"
    _make_archive(
        first_archive,
        books=[product_a, product_b],
        reviews=[review_a, review_b],
    )
    _make_archive(
        second_archive,
        books=[product_b, product_a],
        reviews=[review_b, review_a],
    )
    first = tmp_path / "first"
    second = tmp_path / "second"

    _prepare(first_archive, first, profile="full")
    _prepare(second_archive, second, profile="full")

    assert (first / "products.jsonl").read_bytes() == (
        second / "products.jsonl"
    ).read_bytes()
    assert (first / "reviews.jsonl").read_bytes() == (
        second / "reviews.jsonl"
    ).read_bytes()


def test_invalid_ranges_orphans_and_cover_urls_are_counted(tmp_path: Path) -> None:
    archive = tmp_path / "ranges.zip"
    books = [
        [
            "1",
            "Valid book",
            "",
            "-1",
            "80000",
            "-3",
            "",
            "2",
            "9",
            "-2",
            "",
            "https://evil.example/cover.jpg",
        ],
        [
            "2",
            "Invalid price",
            "",
            "100",
            "not-a-price",
            "0",
            "Khác",
            "0",
            "4",
            "10",
            "",
            "",
        ],
    ]
    reviews = [
        ["1", "10", "", "-1", "private", "5", "Không hay."],
        ["2", "20", "", "0", "private", "5", "Sản phẩm bị loại."],
    ]
    _make_archive(archive, books=books, reviews=reviews)
    output = tmp_path / "snapshot"

    _prepare(archive, output, profile="full")

    product = _read_jsonl(output / "products.jsonl")[0]
    review = _read_jsonl(output / "reviews.jsonl")[0]
    report = json.loads((output / "quality-report.json").read_text(encoding="utf-8"))
    assert product["original_price_vnd"] is None
    assert product["rating"] is None
    assert product["page_count"] is None
    assert product["source_popularity"] is None
    assert product["cover_url"] is None
    assert review["helpful_count"] == 0
    assert report["drop_counts"]["product_invalid_price"] == 1
    assert report["drop_counts"]["review_orphan"] == 1
    assert report["correction_counts"]["product_invalid_rating"] == 1
    assert report["correction_counts"]["product_invalid_page_count"] == 1
    assert report["correction_counts"]["product_cover_url_rejected"] == 1


@pytest.mark.parametrize(
    ("archive_factory", "message"),
    [
        ("missing", "expected exactly one comments.csv"),
        ("duplicate", "expected exactly one comments.csv"),
        ("schema", "required CSV schema mismatch"),
        ("encoding", "archive CSV data is invalid"),
    ],
)
def test_prepare_rejects_wrong_archive_contract(
    tmp_path: Path,
    archive_factory: str,
    message: str,
) -> None:
    archive = tmp_path / f"{archive_factory}.zip"
    _make_invalid_archive(archive, archive_factory)

    with pytest.raises(DatasetPreparationError, match=message):
        _prepare(archive, tmp_path / "snapshot", profile="full")


def test_prepare_verifies_checksum_before_opening_archive(tmp_path: Path) -> None:
    archive = tmp_path / "not-a-zip"
    archive.write_bytes(b"not a zip archive")

    with pytest.raises(DatasetPreparationError, match="archive checksum mismatch"):
        prepare_snapshot(
            archive_path=archive,
            output_dir=tmp_path / "snapshot",
            profile="full",
            expected_sha256="0" * 64,
        )


def test_download_is_atomic_and_removes_bad_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"pinned archive bytes"
    expected = hashlib.sha256(payload).hexdigest()

    class FakeResponse(io.BytesIO):
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        "app.data.tiki_books.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )
    destination = tmp_path / "source.zip"
    assert (
        download_archive(
            destination=destination,
            url="https://example.test/source.zip",
            expected_sha256=expected,
        )
        == destination
    )
    assert destination.read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))

    bad_destination = tmp_path / "bad.zip"
    with pytest.raises(DatasetPreparationError, match="download checksum mismatch"):
        download_archive(
            destination=bad_destination,
            url="https://example.test/source.zip",
            expected_sha256="f" * 64,
        )
    assert not bad_destination.exists()
    assert not list(tmp_path.glob("*.part"))


def test_quality_validation_rejects_tampered_or_failed_report(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    _make_archive(archive)
    snapshot = tmp_path / "snapshot"
    _prepare(archive, snapshot, profile="full")

    products_path = snapshot / "products.jsonl"
    products_path.write_bytes(products_path.read_bytes() + b" ")
    with pytest.raises(SnapshotQualityError, match="artifact hashes"):
        validate_quality_artifacts(snapshot)

    clean_snapshot = tmp_path / "clean"
    _prepare(archive, clean_snapshot, profile="full")
    quality_path = clean_snapshot / "quality-report.json"
    report = json.loads(quality_path.read_text(encoding="utf-8"))
    report["status"] = "fail"
    quality_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(SnapshotQualityError, match="did not pass"):
        validate_quality_artifacts(clean_snapshot)

    forged_snapshot = tmp_path / "forged"
    _prepare(archive, forged_snapshot, profile="full")
    forged_reviews_path = forged_snapshot / "reviews.jsonl"
    forged_reviews = _read_jsonl(forged_reviews_path)
    forged_reviews[0]["content"] = "Liên hệ forged@example.com"
    _write_jsonl(forged_reviews_path, forged_reviews)
    forged_products_path = forged_snapshot / "products.jsonl"
    forged_manifest_path = forged_snapshot / "manifest.json"
    forged_manifest = json.loads(forged_manifest_path.read_text(encoding="utf-8"))
    forged_manifest["reviews_sha256"] = sha256_file(forged_reviews_path)
    forged_manifest["snapshot_sha256"] = snapshot_sha256(
        forged_products_path,
        forged_reviews_path,
    )
    _write_json(forged_manifest_path, forged_manifest)
    forged_quality_path = forged_snapshot / "quality-report.json"
    forged_report = json.loads(forged_quality_path.read_text(encoding="utf-8"))
    forged_report["output_hashes"] = {
        "manifest.json": sha256_file(forged_manifest_path),
        "products.jsonl": sha256_file(forged_products_path),
        "reviews.jsonl": sha256_file(forged_reviews_path),
        "snapshot": forged_manifest["snapshot_sha256"],
    }
    _write_json(forged_quality_path, forged_report)
    with pytest.raises(SnapshotQualityError, match="PII pattern"):
        validate_quality_artifacts(forged_snapshot)


def test_prepare_refuses_unrelated_output_even_with_force(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    _make_archive(archive)
    output = tmp_path / "snapshot"
    output.mkdir()
    (output / "do-not-overwrite.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(SnapshotExistsError):
        _prepare(archive, output, profile="full")
    with pytest.raises(SnapshotExistsError, match="unrelated files"):
        _prepare(archive, output, profile="full", force=True)


def test_prepare_force_prevalidates_existing_artifacts(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    _make_archive(archive)
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "products.jsonl"
    marker.write_text("keep me\n", encoding="utf-8")
    (output / "reviews.jsonl").mkdir()

    with pytest.raises(SnapshotExistsError, match="non-file artifact"):
        _prepare(archive, output, profile="full", force=True)

    assert marker.read_text(encoding="utf-8") == "keep me\n"


def test_prepare_requires_a_source_archive(tmp_path: Path) -> None:
    with pytest.raises(DatasetPreparationError, match="archive does not exist"):
        prepare_snapshot(
            archive_path=tmp_path / "missing.zip",
            output_dir=tmp_path / "snapshot",
            profile="full",
        )


def _prepare(
    archive: Path,
    output: Path,
    *,
    profile: str,
    force: bool = False,
) -> object:
    return prepare_snapshot(
        archive_path=archive,
        output_dir=output,
        profile=profile,  # type: ignore[arg-type]
        force=force,
        expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )


def _make_archive(
    path: Path,
    *,
    books: list[list[str]] | None = None,
    reviews: list[list[str]] | None = None,
) -> None:
    default_books = [
        [
            "1",
            "Sách Một",
            "Tác giả A",
            "100000",
            "80000",
            "20",
            "Tiểu Thuyết",
            "5",
            "4.5",
            "100",
            "NXB A",
            "",
        ]
    ]
    default_reviews = [
        ["1", "101", "Tốt", "2", "private-user", "5", "Đóng gói rất kỹ."]
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "book_data.csv",
            _csv_payload(BOOK_HEADERS, books if books is not None else default_books),
        )
        archive.writestr(
            "comments.csv",
            _csv_payload(
                REVIEW_HEADERS,
                reviews if reviews is not None else default_reviews,
            ),
        )


def _make_invalid_archive(path: Path, variant: str) -> None:
    books = _csv_payload(
        BOOK_HEADERS,
        [
            [
                "1",
                "Sách",
                "",
                "100",
                "90",
                "1",
                "Khác",
                "1",
                "5",
                "10",
                "",
                "",
            ]
        ],
    )
    reviews = _csv_payload(
        REVIEW_HEADERS,
        [["1", "1", "", "0", "private", "5", "Tốt"]],
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("book_data.csv", books)
        if variant in {"duplicate"}:
            archive.writestr("comments.csv", reviews)
        if variant == "duplicate":
            archive.writestr("nested/comments.csv", reviews)
        if variant == "schema":
            archive.writestr("comments.csv", "product_id,comment_id\n1,1\n")
        if variant == "encoding":
            archive.writestr(
                "comments.csv",
                (",".join(REVIEW_HEADERS) + "\n").encode("ascii")
                + b"1,1,,0,private,5,\xff\n",
            )


def _csv_payload(headers: list[str], rows: list[list[str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


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
