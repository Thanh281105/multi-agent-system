"""Prepare a non-overlapping catalog source for the independent task candidate.

This derives products from the pinned normalized full profile. It does not include
review text in the output, import into PostgreSQL, call a model, or overwrite an
output path. Source validation still scans the normalized review file locally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.data.cleaning import CleaningStats
from app.data.contracts import DatasetManifest, NormalizedProduct
from app.data.quality import (
    build_quality_report,
    sha256_file,
    snapshot_sha256,
    validate_quality_artifacts,
)
from app.evaluation.protocol import canonical_sha256

ROOT = Path(__file__).resolve().parents[3]
FULL_SNAPSHOT = (
    ROOT / "output/evaluation-v3/data-expansion-20261012/tiki-books-v4-full"
)
EVAL_SNAPSHOT = ROOT / "data/snapshots/tiki-books-v4-eval"
CANDIDATE_DIR = ROOT / "evaluation/v3/thesis-validation-v1/catalog-pair-candidate-v1"
ADDENDUM_VERSION = "kaggle-v4-thesis-validation-addendum-v1"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_products(path: Path, products: list[NormalizedProduct]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for product in products:
            payload = json.dumps(
                product.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            stream.write(payload + "\n")


def _load_candidate_ids() -> tuple[dict[str, Any], set[str]]:
    manifest_path = CANDIDATE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    without_hash = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if canonical_sha256(without_hash) != manifest.get("manifest_sha256"):
        raise ValueError("candidate manifest hash mismatch")
    for name, expected_hash in manifest.get("files", {}).items():
        path = CANDIDATE_DIR / name
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"candidate file hash mismatch: {name}")

    used_ids: set[str] = set()
    for row in _read_jsonl(CANDIDATE_DIR / "answer-key.jsonl"):
        for book in row["books"].values():
            used_ids.add(str(book["external_id"]))
    return manifest, used_ids


def prepare(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing path: {output_dir}")

    full_manifest, _ = validate_quality_artifacts(FULL_SNAPSHOT)
    eval_manifest, _ = validate_quality_artifacts(EVAL_SNAPSHOT)
    candidate_manifest, candidate_ids = _load_candidate_ids()

    eval_ids = {
        str(row["external_id"])
        for row in _read_jsonl(EVAL_SNAPSHOT / "products.jsonl")
    }
    full_products = [
        NormalizedProduct.model_validate(row)
        for row in _read_jsonl(FULL_SNAPSHOT / "products.jsonl")
    ]
    full_by_id = {product.external_id: product for product in full_products}
    if len(full_by_id) != len(full_products):
        raise ValueError("full profile has duplicate product external IDs")
    if not eval_ids.issubset(full_by_id):
        raise ValueError("evaluation product IDs are missing from full profile")

    products = [
        full_by_id[external_id]
        for external_id in sorted(full_by_id.keys() - eval_ids)
    ]
    addendum_ids = {product.external_id for product in products}
    candidate_product_rows = _read_jsonl(CANDIDATE_DIR / "catalog-products.jsonl")
    candidate_product_ids = [str(row["external_id"]) for row in candidate_product_rows]
    candidate_products = set(candidate_product_ids)
    if len(candidate_product_ids) != len(candidate_products):
        raise ValueError("candidate catalog contains duplicate product IDs")
    if len(candidate_products) != candidate_manifest["eligible_unique_work_groups"]:
        raise ValueError("candidate catalog count differs from its manifest")
    if candidate_products & eval_ids:
        raise ValueError("candidate catalog overlaps the existing eval snapshot")
    if not candidate_ids.issubset(candidate_products):
        raise ValueError("candidate answer key references an absent catalog product")
    if not candidate_products.issubset(addendum_ids):
        raise ValueError("candidate catalog product is absent from the addendum")

    output_dir.mkdir(parents=True)
    products_path = output_dir / "products.jsonl"
    reviews_path = output_dir / "reviews.jsonl"
    _write_products(products_path, products)
    reviews_path.write_bytes(b"")

    manifest_payload = full_manifest.model_dump(mode="json")
    manifest_payload.update(
        {
            "dataset_version": ADDENDUM_VERSION,
            "profile": "full",
            "config_version": "tiki-books-profiles-1.0.0+validation-addendum-v1",
            "sampling_seed": 20261013,
            "sampling_policy": (
                "Normalized full-profile products excluding every external ID in "
                "the pinned eval snapshot; product metadata only, with no review "
                "rows imported."
            ),
            "source_files": ["book_data.csv"],
            "required_product_external_ids": sorted(candidate_ids),
            "product_count": len(products),
            "review_count": 0,
            "products_sha256": sha256_file(products_path),
            "reviews_sha256": sha256_file(reviews_path),
            "snapshot_sha256": snapshot_sha256(products_path, reviews_path),
        }
    )
    manifest = DatasetManifest.model_validate(manifest_payload)
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest.model_dump(mode="json"))

    stats = CleaningStats(
        input_counts=Counter(
            {
                "normalized_full_profile_products": len(full_products),
                "excluded_eval_product_ids": len(eval_ids),
            }
        )
    )
    quality_report = build_quality_report(
        manifest=manifest,
        products=products,
        reviews=[],
        stats=stats,
        output_counts={
            "products": len(products),
            "reviews": 0,
            "candidate_catalog_products": len(candidate_products),
        },
        snapshot_dir=output_dir,
    )
    _write_json(output_dir / "quality-report.json", quality_report.model_dump())
    validate_quality_artifacts(output_dir)

    provenance = {
        "schema_version": "catalog_addendum_provenance_v1",
        "full_snapshot_sha256": full_manifest.snapshot_sha256,
        "eval_snapshot_sha256": eval_manifest.snapshot_sha256,
        "candidate_manifest_sha256": candidate_manifest["manifest_sha256"],
        "candidate_task_count": candidate_manifest["task_count"],
        "candidate_used_product_count": len(candidate_ids),
        "candidate_catalog_product_count": len(candidate_products),
        "excluded_eval_product_count": len(eval_ids),
        "addendum_product_count": len(products),
        "addendum_review_count": 0,
        "addendum_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "addendum_products_sha256": manifest.products_sha256,
        "addendum_snapshot_sha256": manifest.snapshot_sha256,
        "preparation_script_sha256": sha256_file(Path(__file__)),
        "status": "prepared_unimported",
    }
    _write_json(output_dir / "provenance.json", provenance)
    return {"status": provenance["status"], "output_dir": str(output_dir), **provenance}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = prepare(arguments.output.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
