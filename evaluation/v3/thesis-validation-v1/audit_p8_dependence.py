"""Audit clustering and paired discordance in the legacy P8 report artifacts."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_ARTIFACT_DIR = (
    _ROOT / "output/evaluation-v3/heldout-live-v3/p8-publication/scored-artifacts"
)
_ARMS = ("sa_shared_tools_rag", "ma_adaptive_rag")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _find(parent: dict[str, str], item: str) -> str:
    root = item
    while parent[root] != root:
        root = parent[root]
    while parent[item] != item:
        previous = parent[item]
        parent[item] = root
        item = previous
    return root


def _union(parent: dict[str, str], left: str, right: str) -> None:
    left_root = _find(parent, left)
    right_root = _find(parent, right)
    if left_root != right_root:
        parent[right_root] = left_root


def audit(artifact_dir: Path) -> dict[str, Any]:
    answers = _read_json(artifact_dir / "blinded_answers.json").get("answers")
    judgments = _read_json(artifact_dir / "judgments.json").get("judgments")
    entries = _read_json(artifact_dir / "unblinding_key.json").get("entries")
    if not all(isinstance(rows, list) for rows in (answers, judgments, entries)):
        raise ValueError("P8 answer, judgment, or unblinding rows are missing")

    identity_by_answer = {row["opaque_answer_id"]: row for row in entries}
    answer_by_id = {row["opaque_answer_id"]: row for row in answers}
    if len(identity_by_answer) != len(entries) or len(answer_by_id) != len(answers):
        raise ValueError("P8 artifacts contain duplicate opaque answer IDs")
    if set(identity_by_answer) != set(answer_by_id):
        raise ValueError("answer and unblinding rosters differ")

    groups: dict[str, set[str]] = defaultdict(set)
    for answer in answers:
        identity = identity_by_answer[answer["opaque_answer_id"]]
        group_id = identity["work_group_id"]
        groups[group_id]
        for fact in answer.get("rubric_context", {}).get("required_facts", []):
            source_id = fact.get("support_record_id")
            if isinstance(source_id, str) and source_id:
                groups[group_id].add(source_id)

    parent = {group_id: group_id for group_id in groups}
    first_group_by_source: dict[str, str] = {}
    for group_id, source_ids in groups.items():
        for source_id in source_ids:
            first_group = first_group_by_source.setdefault(source_id, group_id)
            _union(parent, group_id, first_group)

    component_sizes: dict[str, int] = defaultdict(int)
    for group_id in groups:
        component_sizes[_find(parent, group_id)] += 1

    paired: dict[tuple[str, int], dict[str, int]] = defaultdict(dict)
    for judgment in judgments:
        identity = identity_by_answer[judgment["opaque_answer_id"]]
        arm = identity["variant_id"]
        score = judgment.get("scores", {}).get("task_completion")
        if arm in _ARMS and score in (0, 1, 0.0, 1.0):
            paired[(identity["conversation_id"], identity["repetition"])][arm] = int(
                score
            )

    complete_pairs = [
        scores for scores in paired.values() if all(arm in scores for arm in _ARMS)
    ]
    p10 = sum(
        scores[_ARMS[0]] == 1 and scores[_ARMS[1]] == 0
        for scores in complete_pairs
    )
    p01 = sum(
        scores[_ARMS[0]] == 0 and scores[_ARMS[1]] == 1
        for scores in complete_pairs
    )
    return {
        "accepted_judgments": len(judgments),
        "unique_conversations": len({row["conversation_id"] for row in entries}),
        "work_groups": len(groups),
        "unique_required_source_records": len(first_group_by_source),
        "overlap_component_count": len(component_sizes),
        "overlap_component_sizes_desc": sorted(component_sizes.values(), reverse=True),
        "paired_conversation_repetitions": len(complete_pairs),
        "raw_p10_sa_success_ma_failure": p10,
        "raw_p01_sa_failure_ma_success": p01,
        "raw_cell_discordance_rate": (p10 + p01) / len(complete_pairs)
        if complete_pairs
        else None,
        "warning": "raw cell discordance is not an independent-group estimate",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=_DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(audit(args.artifact_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
