"""Load and cross-validate frozen evaluation v2 experiment assets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.agents import DEFAULT_AGENT_IDS
from app.evaluation.corpus import (
    LoadedEvaluationCorpusV2,
    load_evaluation_corpus_v2,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v2_models import (
    EvaluationExperimentConfigV2,
    PricingManifestV2,
    RuntimeMode,
)


@dataclass(frozen=True, slots=True)
class LoadedEvaluationExperimentV2:
    config: EvaluationExperimentConfigV2
    corpus: LoadedEvaluationCorpusV2
    pricing: PricingManifestV2
    experiment_sha256: str
    pricing_sha256: str


def load_evaluation_experiment_v2(
    experiment_path: Path,
    corpus_manifest_path: Path,
    base_corpus_path: Path,
    pricing_path: Path,
) -> LoadedEvaluationExperimentV2:
    config = EvaluationExperimentConfigV2.model_validate_json(
        experiment_path.read_text(encoding="utf-8")
    )
    corpus = load_evaluation_corpus_v2(corpus_manifest_path, base_corpus_path)
    pricing = PricingManifestV2.model_validate_json(
        pricing_path.read_text(encoding="utf-8")
    )
    _validate_asset_compatibility(config, corpus, pricing)
    return LoadedEvaluationExperimentV2(
        config=config,
        corpus=corpus,
        pricing=pricing,
        experiment_sha256=canonical_sha256(config),
        pricing_sha256=canonical_sha256(pricing),
    )


def _validate_asset_compatibility(
    config: EvaluationExperimentConfigV2,
    corpus: LoadedEvaluationCorpusV2,
    pricing: PricingManifestV2,
) -> None:
    known_cases = {case.case_id for case in corpus.cases}
    unknown_latency_cases = set(config.latency_case_ids) - known_cases
    if unknown_latency_cases:
        raise ValueError(
            "experiment contains unknown latency cases: "
            f"{sorted(unknown_latency_cases)}"
        )
    if config.warmup_case_id is not None and config.warmup_case_id not in known_cases:
        raise ValueError("experiment warmup case is absent from the corpus")

    variants = {variant.variant_id: variant for variant in config.variants}
    baseline = variants[config.baseline_variant_id]
    if baseline.runtime_mode != RuntimeMode.DETERMINISTIC:
        raise ValueError("experiment baseline must be deterministic")

    supported_agents = set(DEFAULT_AGENT_IDS)
    priced_models = {entry.model for entry in pricing.entries}
    for variant in config.variants:
        unknown_agents = set(variant.enabled_agents) - supported_agents
        if unknown_agents:
            raise ValueError(
                f"variant {variant.variant_id!r} contains unsupported agents: "
                f"{sorted(unknown_agents)}"
            )
        if variant.embedding_backend != "hashed_token_cosine_v1":
            raise ValueError(
                f"variant {variant.variant_id!r} uses an unsupported embedding backend"
            )
        if variant.runtime_mode not in {RuntimeMode.DETERMINISTIC, RuntimeMode.HYBRID}:
            raise ValueError(
                f"variant {variant.variant_id!r} is not executable by this runner"
            )
        for binding in variant.model_bindings:
            if binding.provider != pricing.provider:
                raise ValueError(
                    f"variant {variant.variant_id!r} uses an unpriced provider"
                )
            if binding.model not in priced_models:
                raise ValueError(
                    f"variant {variant.variant_id!r} uses unpriced model "
                    f"{binding.model!r}"
                )
