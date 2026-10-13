"""Prepare a non-executable schedule preview for the catalog validation candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_gold import IdentityFixtureV3, load_evaluation_gold_v3
from app.evaluation.v3_models import EvaluationExperimentConfigV3
from app.evaluation.v3_runner import (
    EvaluationCaseV3,
    EvaluationUserTurnV3,
    execution_case_set_sha256_v3,
)
from app.evaluation.v3_schedule import (
    build_dynamic_validation_schedule_v3,
    pilot_schedule_sha256_v3,
    schedule_algorithm_sha256_v3,
)

ROOT = Path(__file__).resolve().parents[3]
CANDIDATE_DIR = ROOT / "evaluation/v3/thesis-validation-v1/catalog-pair-candidate-v1"
EXPERIMENT_PATH = ROOT / "evaluation/v3/review-successor-v1/experiment.v3.json"
GOLD_PATH = ROOT / "evaluation/v3/review-successor-v1/gold.v3.json"
SPLIT_PATH = ROOT / "evaluation/v3/review-successor-v1/split.v3.json"
RUNTIME_MANIFEST_PATH = ROOT / "output/review-remediation/live-runtime-manifest.v2.json"
OUTPUT_PATH = CANDIDATE_DIR / "validation-schedule-preview-v1.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _git_revision() -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={ROOT.as_posix()}",
            "rev-parse",
            "HEAD",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    revision = completed.stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("current Git revision is not a full commit SHA")
    return revision


def _load_candidate() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = CANDIDATE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    unsigned_manifest = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if canonical_sha256(unsigned_manifest) != manifest.get("manifest_sha256"):
        raise ValueError("candidate manifest digest mismatch")
    for filename, expected_hash in manifest.get("files", {}).items():
        path = CANDIDATE_DIR / filename
        if _sha256(path) != expected_hash:
            raise ValueError(f"candidate file digest mismatch: {filename}")
    prompts = _read_jsonl(CANDIDATE_DIR / "prompts.jsonl")
    if len(prompts) != manifest.get("task_count"):
        raise ValueError("candidate prompt count differs from its manifest")
    case_ids = [str(row.get("case_id", "")) for row in prompts]
    work_group_ids = [str(row.get("work_group_id", "")) for row in prompts]
    if len(case_ids) != len(set(case_ids)) or len(work_group_ids) != len(
        set(work_group_ids)
    ):
        raise ValueError("candidate case and work-group IDs must be unique")
    if any(row.get("split") != "independent_candidate" for row in prompts):
        raise ValueError("candidate prompt roster contains a foreign split")
    return manifest, prompts


def _case_from_prompt(row: dict[str, Any]) -> EvaluationCaseV3:
    identity = IdentityFixtureV3(
        identity_fixture_id="identity_merchant_readonly_v3",
        role="merchant",
        scopes=("ecommerce.read", "merchant.read"),
    )
    case_id = str(row["case_id"])
    return EvaluationCaseV3(
        case_id=case_id,
        work_group_id=str(row["work_group_id"]),
        category="multi_expert_compare_recommendation",
        principal_role="merchant",
        scopes=tuple(scope.value for scope in identity.scopes),
        resolved_product_ids=(),
        user_turns=(
            EvaluationUserTurnV3(
                source_turn_id=f"turn_{case_id}",
                ordinal=1,
                message=str(row["prompt"]),
            ),
        ),
        initial_state={"identity_fixture": identity.model_dump(mode="json")},
    )


def prepare() -> dict[str, Any]:
    manifest, prompts = _load_candidate()
    experiment = EvaluationExperimentConfigV3.model_validate_json(
        EXPERIMENT_PATH.read_text(encoding="utf-8")
    )
    loaded_gold = load_evaluation_gold_v3(
        GOLD_PATH,
        SPLIT_PATH,
        project_root=ROOT,
    )
    warmup_binding = next(
        item
        for item in experiment.pilot_cases
        if item.case_id == experiment.warmup_case_id
    )
    warmup_gold = next(
        item
        for item in loaded_gold.gold.conversations
        if item.conversation_id == warmup_binding.case_id
    )
    if warmup_gold.sandbox_fixture is not None:
        raise ValueError("dynamic validation warmup must remain a development read case")
    warmup_identity = warmup_gold.identity_fixture
    warmup_case = EvaluationCaseV3(
        case_id=warmup_gold.conversation_id,
        work_group_id=warmup_binding.work_group_id,
        category=warmup_gold.category.value,
        principal_role=warmup_identity.role.value,
        scopes=tuple(scope.value for scope in warmup_identity.scopes),
        resolved_product_ids=warmup_gold.product_ids,
        user_turns=tuple(
            EvaluationUserTurnV3(
                source_turn_id=turn.turn_id,
                ordinal=turn.ordinal,
                message=turn.message,
            )
            for turn in warmup_gold.user_turns
        ),
        initial_state={
            "identity_fixture": warmup_identity.model_dump(mode="json")
        },
    )
    candidate_cases = {str(row["case_id"]): _case_from_prompt(row) for row in prompts}
    cases = {warmup_case.case_id: warmup_case, **candidate_cases}
    bindings = tuple(
        (case.case_id, case.work_group_id) for case in candidate_cases.values()
    )
    warmup_case_binding = (warmup_case.case_id, warmup_case.work_group_id)

    revision = _git_revision()
    runtime_manifest_sha256 = (
        _sha256(RUNTIME_MANIFEST_PATH) if RUNTIME_MANIFEST_PATH.is_file() else None
    )
    protocol_material = {
        "schema_version": "catalog_validation_protocol_preview_v1",
        "status": "draft_not_locked",
        "base_experiment_sha256": _sha256(EXPERIMENT_PATH),
        "candidate_manifest_sha256": manifest["manifest_sha256"],
        "candidate_prompt_sha256": manifest["files"]["prompts.jsonl"],
        "answer_key_sha256": manifest["files"]["answer-key.jsonl"],
        "sut_git_revision": revision,
        "runner_sha256": _sha256(ROOT / "app/evaluation/v3_runner.py"),
        "schedule_sha256_source": _sha256(ROOT / "app/evaluation/v3_schedule.py"),
        "schedule_algorithm_sha256": schedule_algorithm_sha256_v3(),
        "runtime_manifest_sha256": runtime_manifest_sha256,
        "warmup_case_binding": warmup_case_binding,
        "measured_case_bindings": bindings,
        "random_seed": experiment.random_seed,
        "variant_ids": tuple(item.variant_id for item in experiment.variants),
    }
    protocol_sha256 = canonical_sha256(protocol_material)
    schedule = build_dynamic_validation_schedule_v3(
        case_bindings=bindings,
        warmup_case_binding=warmup_case_binding,
        variant_ids=tuple(item.variant_id for item in experiment.variants),
        random_seed=experiment.random_seed,
        run_id="run_catalog_validation_preview_v1",
        protocol_sha256=protocol_sha256,
    )
    schedule_sha256 = pilot_schedule_sha256_v3(schedule)
    case_set_sha256 = execution_case_set_sha256_v3(schedule, cases)
    measured = tuple(turn for turn in schedule if turn.observation_id is not None)
    return {
        "schema_version": "catalog_validation_schedule_preview_v1",
        "status": "draft_not_locked_not_executable",
        "protocol_preview_sha256": protocol_sha256,
        "schedule_sha256": schedule_sha256,
        "execution_case_set_sha256": case_set_sha256,
        "sut_git_revision": revision,
        "candidate_manifest_sha256": manifest["manifest_sha256"],
        "candidate_task_count": len(candidate_cases),
        "candidate_answer_key_sha256": manifest["files"]["answer-key.jsonl"],
        "warmup_case_id": warmup_case.case_id,
        "warmup_case_split": "development",
        "scheduled_turn_count": len(schedule),
        "warmup_count": len(schedule) - len(measured),
        "measured_cell_count": len(measured),
        "measured_cells_per_arm": {
            variant.variant_id: sum(
                turn.identity.variant_id == variant.variant_id for turn in measured
            )
            for variant in experiment.variants
        },
        "catalog_case_mode": "merchant",
        "catalog_case_scopes": ["ecommerce.read", "merchant.read"],
        "candidate_resolved_product_id_count": sum(
            len(case.resolved_product_ids) for case in candidate_cases.values()
        ),
        "runtime_manifest_sha256": runtime_manifest_sha256,
        "answer_key_loaded_into_execution_cases": False,
        "api_calls_made": 0,
        "database_calls_made": 0,
        "unresolved_before_lock": [
            "human title and oracle audit",
            "author-overlap analysis plan and power",
            "catalog DatasetSource inventory and snapshot binding",
            "blind judge calibration and budget authorization",
        ],
        "schedule": [turn.model_dump(mode="json") for turn in schedule],
        "protocol_preview": protocol_material,
        "preparation_script_sha256": _sha256(Path(__file__)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    output = parser.parse_args().output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing path: {output}")
    result = prepare()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "schedule"},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
