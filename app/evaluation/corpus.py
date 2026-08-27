"""Load and bind the frozen clean and robustness evaluation corpus."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.evaluation.protocol import canonical_sha256
from app.evaluation.runner import load_corpus
from app.evaluation.v2_models import (
    EvaluationCaseSpecV2,
    EvaluationCorpusManifestV2,
    RobustnessPolicy,
)


@dataclass(frozen=True, slots=True)
class LoadedEvaluationCorpusV2:
    manifest: EvaluationCorpusManifestV2
    cases: tuple[EvaluationCaseSpecV2, ...]
    corpus_sha256: str
    dataset_sha256: str


def load_evaluation_corpus_v2(
    manifest_path: Path,
    base_corpus_path: Path,
) -> LoadedEvaluationCorpusV2:
    manifest = EvaluationCorpusManifestV2.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    base_corpus = load_corpus(base_corpus_path)
    dataset_hash = canonical_sha256(base_corpus)
    if manifest.base_dataset_id != base_corpus.dataset_id:
        raise ValueError("v2 corpus references a different base dataset ID")
    if manifest.base_dataset_sha256 != dataset_hash:
        raise ValueError("v2 corpus base dataset hash mismatch")

    base_cases = {case.case_id: case for case in base_corpus.cases}
    robustness_ids = {case.case_id for case in manifest.robustness_cases}
    overlap = robustness_ids & set(base_cases)
    if overlap:
        raise ValueError(f"robustness case IDs collide with clean cases: {overlap}")

    cases: list[EvaluationCaseSpecV2] = [
        EvaluationCaseSpecV2(
            case_id=case.case_id,
            gold_case=case,
            robustness_policy=RobustnessPolicy.CLEAN,
        )
        for case in base_corpus.cases
    ]
    for definition in manifest.robustness_cases:
        parent = base_cases.get(definition.parent_case_id)
        if parent is None:
            raise ValueError(
                "robustness case references unknown parent: "
                f"{definition.parent_case_id}"
            )
        transformed_gold = parent.model_copy(
            update={
                "case_id": definition.case_id,
                "message": definition.message,
            }
        )
        cases.append(
            EvaluationCaseSpecV2(
                case_id=definition.case_id,
                gold_case=transformed_gold,
                robustness_policy=definition.policy,
                parent_case_id=definition.parent_case_id,
                transform_id=definition.transform_id,
            )
        )

    composite_hash = canonical_sha256(
        {
            "manifest": manifest.model_dump(mode="json"),
            "base_corpus": base_corpus.model_dump(mode="json"),
        }
    )
    return LoadedEvaluationCorpusV2(
        manifest=manifest,
        cases=tuple(cases),
        corpus_sha256=composite_hash,
        dataset_sha256=dataset_hash,
    )
