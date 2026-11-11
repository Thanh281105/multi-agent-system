"""Prepare offline evidence and human work; never run or simulate a live API."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import random
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.source_identity import (
    SourceIdentityError,
    capture_source_identity,
    freeze_source_identity,
)
from app.evaluation.v3_artifacts import BlindedAnswerPacketV3
from app.evaluation.v3_models import EvaluationMetricV3
from app.knowledge.v2_contracts import BookMappingManifest, SourceManifest

_HUMAN_METRICS = (
    EvaluationMetricV3.TASK_COMPLETION,
    EvaluationMetricV3.ANSWERABILITY_ABSTENTION,
    EvaluationMetricV3.CLAIM_SUPPORT,
    EvaluationMetricV3.AUTHORIZATION,
    EvaluationMetricV3.VALID_PLAN,
    EvaluationMetricV3.USEFUL_CONTINUATION,
)


def _write(path: Path, value: Any) -> None:
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(value))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_manifest(output: Path) -> None:
    payload: dict[str, Any] = {
        "schema_version": "offline_preparation_v1",
        "files": [
            {
                "path": p.relative_to(output).as_posix(),
                "sha256": _sha(p),
                "size_bytes": p.stat().st_size,
            }
            for p in sorted(output.rglob("*"))
            if p.is_file()
        ],
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    _write(output / "manifest.json", payload)


def audit_dependencies(root: Path) -> list[dict[str, str | None]]:
    result: dict[str, dict[str, str | None]] = {}
    visited: set[Path] = set()

    def read(path: Path) -> None:
        path = path.resolve()
        if not path.is_relative_to(root.resolve()) or path in visited:
            raise ValueError("dependency_lock_include_invalid")
        visited.add(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r "):
                read(path.parent / line[3:].strip())
                continue
            requirement = Requirement(line)
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            if (
                requirement.url
                or not tuple(requirement.specifier)
                or not all(
                    s.operator == "==" and "*" not in s.version
                    for s in requirement.specifier
                )
            ):
                raise ValueError("dependency_lock_not_exact")
            try:
                installed = importlib.metadata.version(requirement.name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            result[requirement.name] = {
                "package": requirement.name,
                "required": str(requirement.specifier),
                "installed": installed,
                "status": "matched"
                if installed is not None
                and requirement.specifier.contains(installed, prereleases=True)
                else "missing_or_drifted",
            }

    read(root / "requirements-dev.lock")
    return [result[name] for name in sorted(result)]


def prepare_readiness(root: Path, output: Path) -> dict[str, Any]:
    """Inspect locked inputs/dependencies without credentials or a DB connection."""
    from app.evaluation import v3_cli
    from app.evaluation.v3_protocol import evaluation_protocol_sha256_v3
    from app.evaluation.v3_schedule import build_pilot_schedule_v3

    root = root.resolve()
    if output.resolve().is_relative_to(root):
        raise ValueError("readiness_output_must_be_outside_source")
    output.mkdir(parents=True, exist_ok=False)
    blockers = []
    identity = None
    try:
        identity = capture_source_identity(root)
        freeze_source_identity(output / "source-identity.v1.json", identity)
    except SourceIdentityError as exc:
        blockers.append(str(exc))
    dependencies = audit_dependencies(root)
    _write(output / "dependencies.json", dependencies)
    if any(d["status"] != "matched" for d in dependencies):
        blockers.append("dependency_environment_drift")
    arguments = v3_cli._parser().parse_args(
        [
            "validate",
            "--project-root",
            str(root),
            "--experiment",
            str(
                root / "evaluation/v3/mini-generation-luna-judge-v1/experiment.v3.json"
            ),
            "--gold",
            str(root / "evaluation/v3/gold.v3.json"),
            "--split",
            str(root / "evaluation/v3/split.v3.json"),
            "--runtime-manifest",
            str(root / "data/knowledge/books-v1/runtime-manifest.json"),
            "--pricing",
            str(root / "app/shared/pricing-manifest.json"),
        ]
    )
    inputs = v3_cli._load_inputs(arguments)
    protocol_sha = evaluation_protocol_sha256_v3(inputs.protocol)
    schedule = build_pilot_schedule_v3(
        inputs.protocol,
        run_id="readiness_preview",
        protocol_sha256=protocol_sha,
    )
    turns = sum(len(inputs.cases[t.identity.case_id].user_turns) for t in schedule)
    sources_path = root / "data/knowledge/books-v1/sources.json"
    mappings_path = root / "data/knowledge/books-v1/mappings.json"
    sources = SourceManifest.model_validate_json(sources_path.read_bytes())
    mappings = BookMappingManifest.model_validate_json(mappings_path.read_bytes())
    if (
        sources.catalog_source_commit != mappings.catalog_source_commit
        or sources.corpus_version != mappings.corpus_version
    ):
        raise ValueError("corpus_mapping_binding_drift")
    allowed = {s.source_id for s in sources.sources}
    if any(set(m.source_ids) - allowed for m in mappings.mappings):
        raise ValueError("corpus_mapping_source_missing")
    coverage = dict(Counter(m.mapping_status.value for m in mappings.mappings))
    # These are mapping-record hints, not an unchecked join to live DB IDs.
    queue = [
        {
            "mapping_product_id": m.product_id,
            "status": m.mapping_status.value,
            "mapping_evidence": m.mapping_evidence,
            "unresolved_reason": m.ambiguity_reason or m.unmatched_reason,
            "review_state": "not_collected_or_verified",
            "candidate_source_url": None,
            "required_checks": [
                "record_identity",
                "book_vs_non_book",
                "work_vs_edition",
                "source_authority",
                "use_basis",
                "document_relevance",
            ],
        }
        for m in mappings.mappings
        if not m.source_ids
    ]
    with (output / "corpus-review-queue.jsonl").open("xb") as stream:
        for row in queue:
            stream.write(canonical_json_bytes(row))
    plan = {
        "schema_version": "benchmark_plan_v1",
        "stage": "development_pilot",
        "protocol_sha256": protocol_sha,
        "source_identity_sha256": (
            identity.identity_sha256 if identity is not None else None
        ),
        "primary_metric": "task_completion",
        "primary_comparison": ["sa_shared_tools_rag", "ma_adaptive_rag"],
        "remaining_comparisons": "exploratory_unadjusted_not_confirmatory",
        "repeated_measurements": "cluster_by_work_not_independent_tasks",
        "scheduled_observations": len(schedule),
        "scheduled_user_turns": turns,
        "maximum_per_turn_usd": str(
            inputs.protocol.experiment.budget.per_turn_limit_usd
        ),
        "per_turn_reservation_upper_bound_usd": str(
            Decimal(turns) * inputs.protocol.experiment.budget.per_turn_limit_usd
        ),
        "operator_total_budget_usd": None,
        "input_token_bound": (
            inputs.protocol.experiment.budget.max_input_tokens_per_generation
        ),
        "output_token_bound": (
            inputs.protocol.experiment.budget.max_output_tokens_per_generation
        ),
        "requested_generation_model": inputs.protocol.variants[
            0
        ].generation_binding.model,
        "resolved_generation_model": None,
        "publication_requires": [
            "new_independent_holdout",
            "authentic_blind_ratings",
            "live_receipts",
            "calibration_freeze",
        ],
    }
    _write(output / "analysis-plan.json", plan)
    _write(output / "protocol-preview.json", inputs.protocol.model_dump(mode="json"))
    result = {
        "schema_version": "benchmark_readiness_v1",
        "offline_status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "live_status": "not_tested_api_deferred_by_user",
        "provider_calls": 0,
        "database_connections": 0,
        "human_ratings": "not_provided",
        "independent_holdout": "not_provided",
        "coverage": coverage,
        "corpus_source_count": len(sources.sources),
        "corpus_review_queue_count": len(queue),
        "source_manifest_sha256": _sha(sources_path),
        "mapping_manifest_sha256": _sha(mappings_path),
        "measured_live_quality": None,
    }
    _write(output / "readiness.json", result)
    _package_manifest(output)
    return result


def prepare_human_review(packet_path: Path, output: Path) -> dict[str, Any]:
    packet = BlindedAnswerPacketV3.model_validate_json(packet_path.read_bytes())
    if not packet.answers:
        raise ValueError("human_review_packet_empty")
    output.mkdir(parents=True, exist_ok=False)
    for slot, seed in (("reviewer_a", 1701), ("reviewer_b", 2718)):
        folder = output / slot
        folder.mkdir()
        answers = list(packet.answers)
        random.Random(seed).shuffle(answers)
        _write(
            folder / "review-packet.json",
            {
                "schema_version": "blind_review_copy_v1",
                "source_packet_sha256": packet.packet_sha256,
                "answers": [a.model_dump(mode="json") for a in answers],
            },
        )
        with (folder / "ratings.jsonl").open("xb") as stream:
            for answer in answers:
                for metric in _HUMAN_METRICS:
                    stream.write(
                        canonical_json_bytes(
                            {
                                "opaque_answer_id": answer.opaque_answer_id,
                                "metric": metric.value,
                                "reviewer_slot": slot,
                                "score": None,
                                "comment": None,
                            }
                        )
                    )
    result = {
        "status": "awaiting_independent_human_ratings",
        "authenticity": "not_attested",
        "answer_count": packet.answer_count,
        "source_packet_sha256": packet.packet_sha256,
        "submitted_labels": 0,
        "not_a_calibration_freeze": True,
        "instructions": [
            "Assign two distinct real reviewers to the anonymous slots.",
            "Distribute only the respective reviewer folder, never the unblinding key.",
            "Grade independently before seeing model scores or the other review.",
            "Use 0 or 1; leave ungradable labels null and explain missing evidence.",
            "Keep original submissions; a third person adjudicates disagreements.",
            "Reviewer-slot names do not certify that real people have reviewed.",
        ],
    }
    _write(output / "instructions.json", result)
    _package_manifest(output)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--project-root", type=Path, default=Path.cwd())
    inspect.add_argument("--output", type=Path, required=True)
    review = commands.add_parser("human-review")
    review.add_argument("--packet", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inspect":
        result = prepare_readiness(args.project_root, args.output.resolve())
    else:
        result = prepare_human_review(args.packet, args.output.resolve())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
