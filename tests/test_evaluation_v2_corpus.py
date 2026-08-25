"""Frozen evaluation v2 corpus and pricing manifest integrity tests."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from app.evaluation.corpus import load_evaluation_corpus_v2
from app.evaluation.experiment import load_evaluation_experiment_v2
from app.evaluation.models import EvaluationCategory
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v2_models import (
    ModelStage,
    PricingManifestV2,
    RobustnessPolicy,
    RuntimeMode,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_CORPUS_PATH = PROJECT_ROOT / "evaluation" / "cases.v1.json"
CORPUS_V2_PATH = PROJECT_ROOT / "evaluation" / "corpus.v2.json"
EXPERIMENT_PATH = PROJECT_ROOT / "evaluation" / "experiment.v2.json"
PRICING_PATH = (
    PROJECT_ROOT / "evaluation" / "pricing" / "openai-standard-2026-08-25.v2.json"
)


def test_v2_corpus_binds_clean_gold_and_balanced_robustness() -> None:
    corpus = load_evaluation_corpus_v2(CORPUS_V2_PATH, BASE_CORPUS_PATH)

    assert len(corpus.cases) == 44
    assert (
        sum(case.robustness_policy == RobustnessPolicy.CLEAN for case in corpus.cases)
        == 28
    )
    transformed = [
        case
        for case in corpus.cases
        if case.robustness_policy != RobustnessPolicy.CLEAN
    ]
    assert len(transformed) == 16
    assert Counter(case.transform_id for case in transformed) == {
        "typo_noise": 4,
        "polite_paraphrase": 4,
        "irrelevant_distractor": 4,
        "instruction_injection": 4,
    }
    assert len(corpus.corpus_sha256) == len(corpus.dataset_sha256) == 64


def test_transformed_cases_inherit_every_gold_label_except_text_and_id() -> None:
    corpus = load_evaluation_corpus_v2(CORPUS_V2_PATH, BASE_CORPUS_PATH)
    clean = {
        case.case_id: case.gold_case
        for case in corpus.cases
        if case.robustness_policy == RobustnessPolicy.CLEAN
    }

    for transformed in (
        case
        for case in corpus.cases
        if case.robustness_policy != RobustnessPolicy.CLEAN
    ):
        assert transformed.parent_case_id is not None
        parent = clean[transformed.parent_case_id]
        gold = transformed.gold_case
        assert gold.case_id == transformed.case_id
        assert gold.message != parent.message
        assert gold.category == parent.category
        assert gold.accepted_intents == parent.accepted_intents
        assert gold.expected_actions == parent.expected_actions
        assert gold.accepted_statuses == parent.accepted_statuses
        assert gold.relevant_product_ids == parent.relevant_product_ids
        assert gold.answer_assertions == parent.answer_assertions
        assert gold.failure_injection == parent.failure_injection


def test_v2_corpus_rejects_tampered_dataset_hash_and_unknown_parent(
    tmp_path: Path,
) -> None:
    payload = json.loads(CORPUS_V2_PATH.read_text(encoding="utf-8"))
    payload["base_dataset_sha256"] = "f" * 64
    tampered_hash = tmp_path / "tampered_hash.json"
    tampered_hash.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="base dataset hash mismatch"):
        load_evaluation_corpus_v2(tampered_hash, BASE_CORPUS_PATH)

    payload = json.loads(CORPUS_V2_PATH.read_text(encoding="utf-8"))
    payload["robustness_cases"][0]["parent_case_id"] = "missing_parent"
    unknown_parent = tmp_path / "unknown_parent.json"
    unknown_parent.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown parent"):
        load_evaluation_corpus_v2(unknown_parent, BASE_CORPUS_PATH)


def test_pricing_manifest_is_versioned_and_covers_selected_models() -> None:
    pricing = PricingManifestV2.model_validate_json(
        PRICING_PATH.read_text(encoding="utf-8")
    )

    assert pricing.provider == "openai"
    assert pricing.source_url == "https://platform.openai.com/pricing"
    assert {entry.model for entry in pricing.entries} == {
        "gpt-5.4-mini",
        "gpt-5.4-mini-2026-03-17",
        "gpt-5.4-nano",
        "gpt-5.4-nano-2026-03-17",
    }
    assert all(entry.output_per_million_usd > 0 for entry in pricing.entries)
    assert len(canonical_sha256(pricing)) == 64


def test_clean_corpus_still_has_all_thesis_categories() -> None:
    corpus = load_evaluation_corpus_v2(CORPUS_V2_PATH, BASE_CORPUS_PATH)
    clean_categories = Counter(
        case.gold_case.category
        for case in corpus.cases
        if case.robustness_policy == RobustnessPolicy.CLEAN
    )

    assert clean_categories == {category: 4 for category in EvaluationCategory}


def test_experiment_manifest_binds_paired_baseline_and_stage_ablations() -> None:
    experiment = load_evaluation_experiment_v2(
        EXPERIMENT_PATH,
        CORPUS_V2_PATH,
        BASE_CORPUS_PATH,
        PRICING_PATH,
    )

    assert len(experiment.config.variants) == 6
    assert experiment.config.baseline_variant_id == "deterministic_v2"
    assert experiment.config.variants[0].runtime_mode == RuntimeMode.DETERMINISTIC
    assert len(experiment.config.latency_case_ids) == len(EvaluationCategory)
    full = next(
        variant
        for variant in experiment.config.variants
        if variant.variant_id == "hybrid_full"
    )
    assert {binding.stage for binding in full.model_bindings} == {
        ModelStage.ROUTING,
        ModelStage.PLANNING,
        ModelStage.SPECIALIST,
        ModelStage.SYNTHESIS,
    }
    assert {binding.model for binding in full.model_bindings} == {
        "gpt-5.4-nano-2026-03-17",
        "gpt-5.4-mini-2026-03-17",
    }
    enabled_stages = {
        variant.variant_id: {binding.stage for binding in variant.model_bindings}
        for variant in experiment.config.variants
    }
    all_stages = {
        ModelStage.ROUTING,
        ModelStage.PLANNING,
        ModelStage.SPECIALIST,
        ModelStage.SYNTHESIS,
    }
    assert enabled_stages == {
        "deterministic_v2": set(),
        "hybrid_full": all_stages,
        "hybrid_no_router": all_stages - {ModelStage.ROUTING},
        "hybrid_no_planner": all_stages - {ModelStage.PLANNING},
        "hybrid_no_specialist": all_stages - {ModelStage.SPECIALIST},
        "hybrid_no_synthesis": all_stages - {ModelStage.SYNTHESIS},
    }
    assert len(experiment.experiment_sha256) == 64
    assert len(experiment.pricing_sha256) == 64


def test_experiment_loader_rejects_unknown_latency_case(tmp_path: Path) -> None:
    payload = json.loads(EXPERIMENT_PATH.read_text(encoding="utf-8"))
    payload["latency_case_ids"][0] = "unknown_case"
    invalid = tmp_path / "invalid_experiment.json"
    invalid.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown latency cases"):
        load_evaluation_experiment_v2(
            invalid,
            CORPUS_V2_PATH,
            BASE_CORPUS_PATH,
            PRICING_PATH,
        )
