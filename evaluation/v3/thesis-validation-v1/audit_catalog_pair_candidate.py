"""Verify integrity and old-snapshot separation of the catalog-pair candidate."""

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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _normalize_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(
        re.findall(r"[a-z0-9]+", _EDITION_MARKERS.sub(" ", unaccented))
    )


def _work_key(product: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return (
        _normalize_label(str(product.get("name", ""))),
        tuple(
            _normalize_label(str(author))
            for author in product.get("authors", [])
            if str(author).strip()
        ),
    )


def audit(candidate_dir: Path, eval_snapshot: Path) -> dict[str, Any]:
    manifest_path = candidate_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    manifest_payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if _canonical_sha256(manifest_payload) != manifest.get("manifest_sha256"):
        errors.append("candidate manifest digest mismatch")

    for filename, expected in manifest.get("files", {}).items():
        path = candidate_dir / filename
        if not path.is_file() or _sha256(path) != expected:
            errors.append(f"candidate file digest mismatch: {filename}")

    prompts = _read_jsonl(candidate_dir / "prompts.jsonl")
    answer_key = _read_jsonl(candidate_dir / "answer-key.jsonl")
    catalog = _read_jsonl(candidate_dir / "catalog-products.jsonl")
    product_by_id = {str(item["external_id"]): item for item in catalog}
    if len(product_by_id) != len(catalog):
        errors.append("catalog contains duplicate external IDs")
    if len({_work_key(item) for item in catalog}) != len(catalog):
        errors.append("candidate catalog contains duplicate normalized work groups")
    if len(prompts) != len(answer_key):
        errors.append("prompt and answer-key row counts differ")

    used_ids: list[str] = []
    used_work_keys: list[tuple[str, tuple[str, ...]]] = []
    page_tasks = 0
    for index, (prompt, oracle) in enumerate(zip(prompts, answer_key), start=1):
        expected_case_id = f"catalog_pair_{index:04d}"
        if (
            prompt.get("case_id") != expected_case_id
            or oracle.get("case_id") != expected_case_id
            or prompt.get("work_group_id") != oracle.get("work_group_id")
            or prompt.get("split") != "independent_candidate"
        ):
            errors.append(f"task roster mismatch: {expected_case_id}")

        books = oracle.get("books", {})
        if set(books) != {"A", "B"}:
            errors.append(f"two-book oracle missing: {expected_case_id}")
            continue
        normalized_keys: dict[str, tuple[str, tuple[str, ...]]] = {}
        for side in ("A", "B"):
            book = books[side]
            external_id = str(book.get("external_id", ""))
            used_ids.append(external_id)
            source = product_by_id.get(external_id)
            if source is None:
                errors.append(f"oracle product absent from catalog: {expected_case_id}/{side}")
                continue
            for field in ("name", "authors", "price_vnd", "page_count", "rating"):
                if book.get(field) != source.get(field):
                    errors.append(f"oracle/source mismatch: {expected_case_id}/{side}/{field}")
            work_key = _work_key(book)
            normalized_keys[side] = work_key
            used_work_keys.append(work_key)
            if book.get("work_group_key_sha256") != _canonical_sha256(work_key):
                errors.append(f"work-group digest mismatch: {expected_case_id}/{side}")
            prompt_text = prompt.get("prompt", "")
            if (
                json.dumps(book.get("name"), ensure_ascii=False) not in prompt_text
                or json.dumps(", ".join(book.get("authors", [])), ensure_ascii=False)
                not in prompt_text
            ):
                errors.append(f"prompt identity mismatch: {expected_case_id}/{side}")

        if len(normalized_keys) == 2:
            expected_group = "pair_" + _canonical_sha256(
                sorted((normalized_keys["A"], normalized_keys["B"]))
            )[:20]
            if prompt.get("work_group_id") != expected_group:
                errors.append(f"task-group digest mismatch: {expected_case_id}")

        a, b = books["A"], books["B"]
        expected_values = [
            {"field": "A.price_vnd", "value": a["price_vnd"]},
            {"field": "A.rating", "value": a["rating"]},
            {"field": "B.price_vnd", "value": b["price_vnd"]},
            {"field": "B.rating", "value": b["rating"]},
            {
                "field": "cheaper_book",
                "value": "tie" if a["price_vnd"] == b["price_vnd"] else "A" if a["price_vnd"] < b["price_vnd"] else "B",
            },
            {
                "field": "higher_rating_book",
                "value": "tie" if a["rating"] == b["rating"] else "A" if a["rating"] > b["rating"] else "B",
            },
        ]
        if oracle.get("requires_page_count"):
            page_tasks += 1
            expected_values.extend(
                (
                    {"field": "A.page_count", "value": a["page_count"]},
                    {"field": "B.page_count", "value": b["page_count"]},
                )
            )
            if any(
                type(books[side].get("page_count")) is not int
                or books[side]["page_count"] <= 0
                for side in ("A", "B")
            ):
                errors.append(f"page-count oracle invalid: {expected_case_id}")
        if oracle.get("required_values") != expected_values:
            errors.append(f"required-value calculation mismatch: {expected_case_id}")

    if len(set(used_ids)) != len(used_ids):
        errors.append("a catalog product is reused across candidate tasks")
    if len(set(used_work_keys)) != len(used_work_keys):
        errors.append("a normalized title-author group is reused")
    if len({row.get("work_group_id") for row in prompts}) != len(prompts):
        errors.append("task work-group IDs are not unique")

    eval_manifest_path = eval_snapshot / "manifest.json"
    eval_manifest = json.loads(eval_manifest_path.read_text(encoding="utf-8"))
    eval_products_path = eval_snapshot / "products.jsonl"
    eval_products_sha256 = _sha256(eval_products_path)
    if eval_manifest.get("products_sha256") != eval_products_sha256:
        errors.append("old eval snapshot product digest mismatch")
    eval_products = _read_jsonl(eval_products_path)
    eval_ids = {str(item["external_id"]) for item in eval_products}
    eval_work_keys = {_work_key(item) for item in eval_products}
    product_id_overlap = len(set(used_ids) & eval_ids)
    work_key_overlap = len(set(used_work_keys) & eval_work_keys)
    if product_id_overlap:
        errors.append("candidate product IDs overlap the old eval snapshot")
    if work_key_overlap:
        errors.append("candidate title-author groups overlap the old eval snapshot")

    report = {
        "schema_version": "catalog_pair_candidate_structural_audit_v1",
        "status": "structural_pass_not_human_or_external_validation" if not errors else "failed",
        "candidate_status_unchanged": manifest.get("status"),
        "candidate_manifest_sha256": manifest.get("manifest_sha256"),
        "audit_script_sha256": _sha256(Path(__file__)),
        "old_eval_snapshot_manifest_sha256": _sha256(eval_manifest_path),
        "old_eval_products_sha256": eval_products_sha256,
        "candidate_file_sha256": {
            name: _sha256(candidate_dir / name) for name in manifest.get("files", {})
        },
        "task_count": len(prompts),
        "answer_key_count": len(answer_key),
        "catalog_product_count": len(catalog),
        "unique_used_product_count": len(set(used_ids)),
        "unique_used_title_author_count": len(set(used_work_keys)),
        "page_count_task_count": page_tasks,
        "old_eval_product_id_overlap": product_id_overlap,
        "old_eval_title_author_overlap": work_key_overlap,
        "error_count": len(errors),
        "errors": errors[:100],
        "limitations": [
            "title/edition ambiguity still needs human review",
            "oracle values are checked against the same snapshot, not an independent authority",
            "the candidate shares the Tiki/Kaggle source family and cannot validate external-domain generalization",
            "catalog tasks do not measure knowledge document recall",
        ],
    }
    return report


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=root / "evaluation/v3/thesis-validation-v1/catalog-pair-candidate-v1",
    )
    parser.add_argument(
        "--eval-snapshot",
        type=Path,
        default=root / "data/snapshots/tiki-books-v4-eval",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.candidate_dir, args.eval_snapshot)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized, encoding="utf-8", newline="\n")
    print(serialized, end="")
    return 0 if report["status"].startswith("structural_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
