"""Audit repeated author labels across the catalog-pair task candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.evaluation.protocol import canonical_sha256

ROOT = Path(__file__).resolve().parents[3]
CANDIDATE_DIR = ROOT / "evaluation/v3/thesis-validation-v1/catalog-pair-candidate-v1"
UNKNOWN_AUTHOR_LABELS = {"", ".", "na", "n a", "none", "unknown", "khong ro", "nhieu tac gia", "khuyet danh"}


def _normalize_author(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", unaccented))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def audit() -> dict[str, Any]:
    manifest_path = CANDIDATE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if canonical_sha256(manifest_payload) != manifest.get("manifest_sha256"):
        raise ValueError("candidate manifest digest mismatch")

    for filename, expected_hash in manifest.get("files", {}).items():
        path = CANDIDATE_DIR / filename
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"candidate file digest mismatch: {filename}")

    rows = _read_jsonl(CANDIDATE_DIR / "answer-key.jsonl")
    task_ids = [str(row["case_id"]) for row in rows]
    work_group_hashes: list[str] = []
    product_ids: list[str] = []
    task_authors: list[set[str]] = []
    author_tasks: dict[str, set[int]] = defaultdict(set)
    placeholder_mentions = 0

    for task_index, row in enumerate(rows):
        authors: set[str] = set()
        for book in row["books"].values():
            product_ids.append(str(book["external_id"]))
            work_group_hashes.append(str(book["work_group_key_sha256"]))
            for raw_author in book.get("authors", []):
                author = _normalize_author(str(raw_author))
                if author in UNKNOWN_AUTHOR_LABELS:
                    placeholder_mentions += 1
                    continue
                authors.add(author)
        task_authors.append(authors)
        for author in authors:
            author_tasks[author].add(task_index)

    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for task_indexes in author_tasks.values():
        if len(task_indexes) > 1:
            first = next(iter(task_indexes))
            for task_index in task_indexes:
                union(first, task_index)

    component_sizes = Counter(find(index) for index in range(len(rows)))
    repeated_authors = {
        author: task_indexes
        for author, task_indexes in author_tasks.items()
        if len(task_indexes) > 1
    }
    linked_task_indexes = set().union(*repeated_authors.values()) if repeated_authors else set()
    component_histogram = Counter(component_sizes.values())

    return {
        "schema_version": "catalog_pair_author_overlap_audit_v1",
        "status": "dependency_screen_only",
        "candidate_manifest_sha256": manifest["manifest_sha256"],
        "answer_key_sha256": hashlib.sha256(
            (CANDIDATE_DIR / "answer-key.jsonl").read_bytes()
        ).hexdigest(),
        "task_count": len(rows),
        "unique_task_ids": len(set(task_ids)),
        "unique_work_groups": len(set(work_group_hashes)),
        "unique_product_ids": len(set(product_ids)),
        "placeholder_author_mentions_excluded": placeholder_mentions,
        "normalized_author_labels": len(author_tasks),
        "author_labels_on_multiple_tasks": len(repeated_authors),
        "tasks_linked_by_a_repeated_author": len(linked_task_indexes),
        "author_overlap_component_count": len(component_sizes),
        "component_size_histogram": {
            str(size): count for size, count in sorted(component_histogram.items())
        },
        "largest_component_tasks": max(component_sizes.values(), default=0),
        "largest_component_share": round(
            max(component_sizes.values(), default=0) / len(rows), 6
        )
        if rows
        else 0,
        "interpretation": (
            "Components connect tasks that share a normalized non-placeholder author. "
            "This is a dependence warning, not an effective sample size or a proof "
            "that every connected task has correlated errors. Author strings may "
            "combine coauthors, and aliases or editions still need human review."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=CANDIDATE_DIR / "author-overlap-audit.json",
    )
    output = parser.parse_args().output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing path: {output}")
    result = audit()
    result["audit_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
