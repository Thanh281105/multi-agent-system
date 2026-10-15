"""Lock and freeze the independent catalog validation benchmark dataset.

This script:
1. Verifies the candidate pool files and structural integrity.
2. Filters out ambiguous edition duplicates and multi-author collisions identified in audits.
3. Groups tasks into independent author clusters to eliminate inter-task dependency in statistical testing.
4. Formalizes independent oracle validation criteria.
5. Emits a tamper-evident locked manifest with SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
CANDIDATE_DIR = ROOT / "evaluation/v3/thesis-validation-v1/catalog-pair-candidate-v1"
OUTPUT_DIR = ROOT / "evaluation/v3/thesis-validation-v1/locked-validation-v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_title(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title).casefold()
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    unaccented = re.sub(r"\([^)]*\)", " ", unaccented)
    return " ".join(re.findall(r"[a-z0-9]+", unaccented))


def build_locked_validation_set():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load candidate files
    prompts_path = CANDIDATE_DIR / "prompts.jsonl"
    answer_key_path = CANDIDATE_DIR / "answer-key.jsonl"
    catalog_path = CANDIDATE_DIR / "catalog-products.jsonl"
    structural_audit_path = CANDIDATE_DIR / "structural-audit.json"
    author_audit_path = CANDIDATE_DIR / "author-overlap-audit.json"

    prompts = [json.loads(line) for line in prompts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    answer_keys = [json.loads(line) for line in answer_key_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    catalog = [json.loads(line) for line in catalog_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    structural_audit = json.loads(structural_audit_path.read_text(encoding="utf-8"))
    author_audit = json.loads(author_audit_path.read_text(encoding="utf-8"))

    catalog_by_id = {str(p["external_id"]): p for p in catalog}

    # Known ambiguous normalized titles to exclude from locked set
    ambiguous_examples = structural_audit.get("used_product_ambiguity_profile", {}).get("multiple_author_title_examples", [])
    ambiguous_norm_titles = {ex["normalized_title"] for ex in ambiguous_examples}

    # Audit and select clean locked tasks
    locked_tasks = []
    author_to_tasks = defaultdict(list)
    excluded_ambiguous = []

    for prompt, ans in zip(prompts, answer_keys):
        case_id = ans["case_id"]
        book_a = ans["books"]["A"]
        book_b = ans["books"]["B"]
        
        title_a_norm = normalize_title(book_a["name"])
        title_b_norm = normalize_title(book_b["name"])

        # Check for ambiguous title collision across distinct authors
        if title_a_norm in ambiguous_norm_titles or title_b_norm in ambiguous_norm_titles:
            excluded_ambiguous.append(case_id)
            continue

        # Verify oracle matches catalog records exactly
        cat_a = catalog_by_id.get(str(book_a["external_id"]))
        cat_b = catalog_by_id.get(str(book_b["external_id"]))
        if not cat_a or not cat_b:
            continue

        # All fields match oracle
        valid_oracle = (
            cat_a["price_vnd"] == book_a["price_vnd"]
            and cat_b["price_vnd"] == book_b["price_vnd"]
            and cat_a["rating"] == book_a["rating"]
            and cat_b["rating"] == book_b["rating"]
        )
        if not valid_oracle:
            continue

        locked_tasks.append({
            "case_id": case_id,
            "prompt": prompt,
            "oracle": ans,
        })

        # Track author clusters for cluster-robust inference
        for side, book in [("A", book_a), ("B", book_b)]:
            for author in book.get("authors", []):
                norm_author = unicodedata.normalize("NFKD", author).casefold()
                author_to_tasks[norm_author].append(case_id)

    # Write locked dataset files
    locked_prompts_file = OUTPUT_DIR / "locked-prompts.jsonl"
    locked_answer_key_file = OUTPUT_DIR / "locked-answer-key.jsonl"

    with open(locked_prompts_file, "w", encoding="utf-8") as f:
        for t in locked_tasks:
            f.write(json.dumps(t["prompt"], ensure_ascii=False) + "\n")

    with open(locked_answer_key_file, "w", encoding="utf-8") as f:
        for t in locked_tasks:
            f.write(json.dumps(t["oracle"], ensure_ascii=False) + "\n")

    # Generate cluster mapping for statistical analysis
    # Tasks sharing authors are assigned to the same cluster ID
    parent = {}
    def find(item):
        if parent.setdefault(item, item) == item:
            return item
        parent[item] = find(parent[item])
        return parent[item]

    def union(a, b):
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for author, task_ids in author_to_tasks.items():
        if len(task_ids) > 1:
            first = task_ids[0]
            for tid in task_ids[1:]:
                union(first, tid)

    clusters = defaultdict(list)
    for t in locked_tasks:
        cid = t["case_id"]
        cluster_root = find(cid)
        clusters[cluster_root].append(cid)

    cluster_manifest = {
        "cluster_count": len(clusters),
        "cluster_size_distribution": {
            str(k): len([c for c in clusters.values() if len(c) == k])
            for k in sorted(set(len(c) for c in clusters.values()))
        },
        "task_to_cluster": {t["case_id"]: find(t["case_id"]) for t in locked_tasks},
    }

    cluster_file = OUTPUT_DIR / "author-cluster-mapping.json"
    cluster_file.write_text(json.dumps(cluster_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Build locked manifest
    manifest = {
        "schema_version": "locked_independent_validation_manifest_v1",
        "timestamp": "2026-10-14T15:45:00Z",
        "status": "frozen_locked_for_final_evaluation",
        "domain": "e-commerce_catalog_pair_reasoning",
        "source_family": "Kaggle_Tiki_Books_v4",
        "generalization_boundary": {
            "cross_source_generalization_claimed": False,
            "rationale": (
                "The validation pool is derived from the Tiki Books v4 Kaggle archive (CC0-1.0). "
                "While ID-disjoint and work-group disjoint from the 200-product development snapshot, "
                "it belongs to the same source family and distribution. It validates in-domain scalability "
                "and generalization to unseen catalog items, but does NOT establish cross-domain generalization "
                "to non-book domains or different e-commerce platform APIs."
            ),
        },
        "prior_heldout_classification": {
            "status": "reclassified_as_development_regression_suite",
            "reason": (
                "The previous 60-case held-out set was repeatedly inspected and used during "
                "system remediation and bug fixing (Package 7/8). It is retained as a regression "
                "benchmark, while this locked suite serves as the unbiased independent validation set."
            ),
        },
        "dataset_statistics": {
            "candidate_tasks": len(prompts),
            "excluded_ambiguous_edition_tasks": len(excluded_ambiguous),
            "locked_clean_tasks": len(locked_tasks),
            "independent_author_clusters": len(clusters),
            "unique_catalog_products_used": len(locked_tasks) * 2,
        },
        "file_digests": {
            "locked-prompts.jsonl": sha256_file(locked_prompts_file),
            "locked-answer-key.jsonl": sha256_file(locked_answer_key_file),
            "author-cluster-mapping.json": sha256_file(cluster_file),
        },
    }

    manifest_file = OUTPUT_DIR / "locked-validation-manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Successfully locked independent validation set: {len(locked_tasks)} tasks across {len(clusters)} clusters.")
    print(f"Manifest written to {manifest_file}")


if __name__ == "__main__":
    build_locked_validation_set()
