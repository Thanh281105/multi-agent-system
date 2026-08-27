"""Build verified comparisons, robustness summaries, and evaluation bundles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.evaluation.artifacts import validate_bundle, write_bundle
from app.evaluation.comparison import compare_variants, summarize_robustness
from app.evaluation.execution import EvaluationExecutionV2
from app.evaluation.protocol import (
    comparison_seed_v2,
    expected_comparison_keys_v2,
    protocol_sha256,
)
from app.evaluation.v2_models import (
    ComparisonOmissionV2,
    EvaluationBundleManifestV2,
    EvaluationPhase,
    PairedComparisonV2,
    PricingManifestV2,
    RobustnessPolicy,
    RobustnessSummaryV2,
)


@dataclass(frozen=True, slots=True)
class EvaluationAnalysisV2:
    comparisons: tuple[PairedComparisonV2, ...]
    omissions: tuple[ComparisonOmissionV2, ...]
    robustness: tuple[RobustnessSummaryV2, ...]


@dataclass(frozen=True, slots=True)
class CompletedEvaluationV2:
    manifest: EvaluationBundleManifestV2
    analysis: EvaluationAnalysisV2


def analyze_evaluation_v2(execution: EvaluationExecutionV2) -> EvaluationAnalysisV2:
    protocol = execution.protocol
    comparisons: list[PairedComparisonV2] = []
    omissions: list[ComparisonOmissionV2] = []
    for baseline_id, candidate_id, metric, phase in expected_comparison_keys_v2(
        protocol
    ):
        seed = comparison_seed_v2(
            protocol.random_seed,
            baseline_id,
            candidate_id,
            metric,
            phase,
        )
        try:
            comparison = compare_variants(
                execution.observations,
                baseline_variant_id=baseline_id,
                candidate_variant_id=candidate_id,
                metric=metric,
                phase=phase,
                bootstrap_samples=protocol.bootstrap_samples,
                random_seed=seed,
            )
        except ValueError as exc:
            if "no comparable paired values" not in str(exc):
                raise
            omissions.append(
                ComparisonOmissionV2(
                    protocol_sha256=protocol_sha256(protocol),
                    baseline_variant_id=baseline_id,
                    candidate_variant_id=candidate_id,
                    metric=metric,
                    phase=phase,
                    reason="no_comparable_paired_values",
                )
            )
        else:
            comparisons.append(comparison)

    robustness = (
        tuple(
            summarize_robustness(
                execution.observations,
                variant_id=variant.variant_id,
            )
            for variant in protocol.variants
        )
        if any(
            observation.robustness_policy != RobustnessPolicy.CLEAN
            and observation.phase == EvaluationPhase.CORRECTNESS
            for observation in execution.observations
        )
        else ()
    )
    return EvaluationAnalysisV2(
        comparisons=tuple(comparisons),
        omissions=tuple(omissions),
        robustness=robustness,
    )


def write_evaluation_bundle_v2(
    output_directory: Path,
    *,
    run_id: str,
    execution: EvaluationExecutionV2,
    pricing: PricingManifestV2,
    created_at: datetime | None = None,
) -> CompletedEvaluationV2:
    analysis = analyze_evaluation_v2(execution)
    manifest = write_bundle(
        output_directory,
        run_id=run_id,
        protocol=execution.protocol,
        observations=execution.observations,
        comparisons=analysis.comparisons,
        omissions=analysis.omissions,
        robustness=analysis.robustness,
        pricing=pricing,
        created_at=created_at or datetime.now(UTC),
        completion_status="complete",
    )
    validated = validate_bundle(output_directory)
    if validated != manifest:
        raise ValueError("published evaluation bundle failed round-trip validation")
    return CompletedEvaluationV2(manifest=manifest, analysis=analysis)
