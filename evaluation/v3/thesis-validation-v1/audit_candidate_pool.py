"""Count unseen, normalized Tiki product groups available for validation planning."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

_EDITION_MARKERS = re.compile(
    r"\([^)]*(?:tai ban|bia cung|bia mem|tang kem|an ban|reprint|edition|"
    r"hardcover|paperback)[^)]*\)",
    re.IGNORECASE,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    without_edition = _EDITION_MARKERS.sub(" ", unaccented)
    return " ".join(re.findall(r"[a-z0-9]+", without_edition))


def _safe_catalog_label(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 240
        and not any(
            ord(character) < 32 and character not in "\t"
            for character in value
        )
    )


def _prompt_eligible(product: dict[str, Any]) -> bool:
    authors = product.get("authors")
    rating = product.get("rating")
    return (
        _safe_catalog_label(product.get("name"))
        and isinstance(authors, list)
        and bool(authors)
        and all(_safe_catalog_label(author) for author in authors)
        and type(product.get("price_vnd")) is int
        and isinstance(rating, (int, float))
    )


def audit_candidate_pool(full_snapshot: Path, eval_snapshot: Path) -> dict[str, Any]:
    manifest = json.loads(
        (full_snapshot / "manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("profile") != "full":
        raise ValueError("candidate snapshot must use the full profile")

    products_path = full_snapshot / "products.jsonl"
    reviews_path = full_snapshot / "reviews.jsonl"
    if _sha256(products_path) != manifest.get("products_sha256"):
        raise ValueError("full snapshot product hash does not match its manifest")
    if _sha256(reviews_path) != manifest.get("reviews_sha256"):
        raise ValueError("full snapshot review hash does not match its manifest")

    eval_products = _read_jsonl(eval_snapshot / "products.jsonl")
    exposed_ids = {str(product["external_id"]) for product in eval_products}

    def work_key(product: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        return (
            _normalize_label(product["name"]),
            tuple(_normalize_label(author) for author in product["authors"]),
        )

    exposed_work_groups = {work_key(product) for product in eval_products}
    id_disjoint = [
        product
        for product in _read_jsonl(products_path)
        if str(product["external_id"]) not in exposed_ids
    ]
    overlapping_work_groups = {
        work_key(product) for product in id_disjoint
    } & exposed_work_groups
    overlapping_work_records = [
        product for product in id_disjoint if work_key(product) in exposed_work_groups
    ]
    candidates = [
        product
        for product in id_disjoint
        if work_key(product) not in exposed_work_groups
    ]

    groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for product in candidates:
        key = work_key(product)
        current = groups.get(key)
        if current is None or str(product["external_id"]) < str(
            current["external_id"]
        ):
            groups[key] = product

    unique_groups = tuple(groups.values())
    prompt_eligible_groups: dict[
        tuple[str, tuple[str, ...]], dict[str, Any]
    ] = {}
    for product in candidates:
        if not _prompt_eligible(product):
            continue
        key = work_key(product)
        current = prompt_eligible_groups.get(key)
        if current is None or str(product["external_id"]) < str(
            current["external_id"]
        ):
            prompt_eligible_groups[key] = product
    eligible_unique_groups = tuple(prompt_eligible_groups.values())
    return {
        "schema_version": "thesis_validation_candidate_pool_v1",
        "grouping_rule": (
            "NFKD_casefold_unaccented_title_authors_"
            "strip_parenthetical_edition_markers_v1"
        ),
        "excluded_eval_external_id_count": len(exposed_ids),
        "excluded_eval_work_group_count": len(exposed_work_groups),
        "excluded_duplicate_work_group_count": len(overlapping_work_groups),
        "excluded_duplicate_work_record_count": len(overlapping_work_records),
        "id_disjoint_candidate_record_count": len(id_disjoint),
        "candidate_product_record_count": len(candidates),
        "candidate_title_author_group_count": len(unique_groups),
        "prompt_eligible_title_author_group_count": len(eligible_unique_groups),
        "prompt_eligible_group_count_with_page_count": sum(
            type(product.get("page_count")) is int and product["page_count"] > 0
            for product in eligible_unique_groups
        ),
        "prompt_eligibility_rule": (
            "same name, author, price, and rating filters as "
            "build_catalog_pair_candidate.py"
        ),
        "group_count_with_value": {
            "price_vnd": sum(
                product.get("price_vnd") is not None for product in unique_groups
            ),
            "rating": sum(
                product.get("rating") is not None for product in unique_groups
            ),
            "source_review_count": sum(
                product.get("source_review_count") is not None
                for product in unique_groups
            ),
            "page_count": sum(
                product.get("page_count") is not None for product in unique_groups
            ),
        },
        "full_profile": {
            "snapshot_sha256": manifest["snapshot_sha256"],
            "products_sha256": manifest["products_sha256"],
            "reviews_sha256": manifest["reviews_sha256"],
            "raw_archive_sha256": manifest["raw_archive_sha256"],
            "product_count": manifest["product_count"],
            "review_count": manifest["review_count"],
        },
    }


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-snapshot",
        type=Path,
        default=(
            root
            / "output/evaluation-v3/data-expansion-20261012/tiki-books-v4-full"
        ),
    )
    parser.add_argument(
        "--eval-snapshot",
        type=Path,
        default=root / "data/snapshots/tiki-books-v4-eval",
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = json.dumps(
        audit_candidate_pool(arguments.full_snapshot, arguments.eval_snapshot),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if arguments.output is not None:
        arguments.output.write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
