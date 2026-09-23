"""Strict, grouped, paired analysis for the additive Evaluation v3 protocol."""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Sequence
from itertools import combinations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_gold import (
    EvaluationSplitManifestV3,
    EvaluationSplitV3,
)
from app.evaluation.v3_models import (
    PACKAGE7_METRICS,
    PACKAGE7_VARIANT_ORDER,
    EvaluationMetricV3,
    EvaluationObservationV3,
    EvaluationProtocolV3,
    ObservationIdentityV3,
    RepeatDecisionV3,
    ScheduledTurnKindV3,
    VariantIdV3,
    canonical_observation_id_v3,
)
from app.evaluation.v3_protocol import (
    evaluation_protocol_sha256_v3,
    validate_evaluation_protocol_v3,
)

_SHA256 = r"^[a-f0-9]{64}$"
_TIE_TOLERANCE = 1e-12

MetricDirectionV3 = Literal["higher_is_better", "lower_is_better"]
CompletionIssueCodeV3 = Literal[
    "missing_cells",
    "ambiguous_work_group",
    "invalid_ledger",
    "environment_stop",
    "budget_stop",
]

METRIC_DIRECTIONS_V3: dict[EvaluationMetricV3, MetricDirectionV3] = {
    EvaluationMetricV3.TASK_COMPLETION: "higher_is_better",
    EvaluationMetricV3.ANSWERABILITY_ABSTENTION: "higher_is_better",
    EvaluationMetricV3.CLAIM_SUPPORT: "higher_is_better",
    EvaluationMetricV3.CITATION_PRECISION: "higher_is_better",
    EvaluationMetricV3.CITATION_COVERAGE: "higher_is_better",
    EvaluationMetricV3.DOCUMENT_RECALL: "higher_is_better",
    EvaluationMetricV3.AUTHORIZATION: "higher_is_better",
    EvaluationMetricV3.VALID_PLAN: "higher_is_better",
    EvaluationMetricV3.USEFUL_CONTINUATION: "higher_is_better",
    EvaluationMetricV3.DUPLICATE_DISPATCH: "lower_is_better",
    EvaluationMetricV3.END_TO_END_LATENCY_MS: "lower_is_better",
    EvaluationMetricV3.TOTAL_TOKENS: "lower_is_better",
    EvaluationMetricV3.EFFECTIVE_COST_USD: "lower_is_better",
}


class FrozenAnalysisContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ArtifactBindingsV3(FrozenAnalysisContractV3):
    """Immutable identities carried by every Evaluation v3 result artifact."""

    protocol_sha256: str = Field(pattern=_SHA256)
    gold_sha256: str = Field(pattern=_SHA256)
    split_sha256: str = Field(pattern=_SHA256)
    schedule_sha256: str = Field(pattern=_SHA256)
    repeat_decision_sha256: str = Field(pattern=_SHA256)
    evaluator_configuration_sha256: str = Field(pattern=_SHA256)
    judge_prompt_sha256: str = Field(pattern=_SHA256)
    judge_schema_sha256: str = Field(pattern=_SHA256)
    tool_contract_sha256: str = Field(pattern=_SHA256)
    corpus_sha256: str = Field(pattern=_SHA256)
    index_sha256: str = Field(pattern=_SHA256)
    embedding_model_dimensions_sha256: str = Field(pattern=_SHA256)
    pricing_manifest_sha256: str = Field(pattern=_SHA256)
    usage_ledger_sha256: str = Field(pattern=_SHA256)
    rubric_sha256: str = Field(pattern=_SHA256)


class EvaluationStopV3(FrozenAnalysisContractV3):
    code: Literal["environment_stop", "budget_stop"]
    affected_identifiers: tuple[str, ...] = Field(min_length=1)
    message: str = Field(min_length=1, max_length=1_000)


class CompletionIssueV3(FrozenAnalysisContractV3):
    code: CompletionIssueCodeV3
    identifiers: tuple[str, ...] = Field(min_length=1)
    message: str = Field(min_length=1, max_length=1_000)


class ConfidenceIntervalV3(FrozenAnalysisContractV3):
    method: Literal["work_group_percentile_bootstrap"] = (
        "work_group_percentile_bootstrap"
    )
    confidence: float = Field(default=0.95, ge=0.95, le=0.95)
    lower: float
    upper: float
    bootstrap_samples: int = Field(ge=100)
    random_seed: int = Field(ge=0, le=2**32 - 1)

    @model_validator(mode="after")
    def validate_bounds(self) -> ConfidenceIntervalV3:
        if self.lower > self.upper:
            raise ValueError("confidence interval lower bound exceeds upper bound")
        return self


class WorkGroupDeltaV3(FrozenAnalysisContractV3):
    work_group_id: str
    conversation_count: int = Field(ge=1)
    paired_repetition_count: int = Field(ge=1)
    baseline_mean: float
    candidate_mean: float
    delta: float


class PairedMetricComparisonV3(FrozenAnalysisContractV3):
    schema_version: Literal["3.0"] = "3.0"
    baseline_variant_id: VariantIdV3
    candidate_variant_id: VariantIdV3
    metric: EvaluationMetricV3
    direction: MetricDirectionV3
    paired_work_group_count: int = Field(ge=0)
    paired_conversation_count: int = Field(ge=0)
    paired_observation_count: int = Field(ge=0)
    baseline_mean: float | None = None
    candidate_mean: float | None = None
    mean_delta: float | None = None
    median_delta: float | None = None
    candidate_wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    candidate_losses: int = Field(ge=0)
    confidence_interval: ConfidenceIntervalV3 | None = None
    work_groups: tuple[WorkGroupDeltaV3, ...] = ()
    missing_value_observation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_comparison(self) -> PairedMetricComparisonV3:
        if self.baseline_variant_id == self.candidate_variant_id:
            raise ValueError("paired comparison requires distinct variants")
        if self.direction != METRIC_DIRECTIONS_V3[self.metric]:
            raise ValueError("metric direction does not match Evaluation v3")
        if self.paired_work_group_count != len(self.work_groups):
            raise ValueError("paired work-group count does not match detail rows")
        if self.paired_conversation_count != sum(
            item.conversation_count for item in self.work_groups
        ):
            raise ValueError("paired conversation count does not match detail rows")
        if self.paired_observation_count != sum(
            item.paired_repetition_count for item in self.work_groups
        ):
            raise ValueError("paired observation count does not match detail rows")
        statistics_values = (
            self.baseline_mean,
            self.candidate_mean,
            self.mean_delta,
            self.median_delta,
        )
        if self.work_groups:
            if any(value is None for value in statistics_values):
                raise ValueError("non-empty paired results require summary statistics")
            if self.confidence_interval is None:
                raise ValueError(
                    "non-empty paired results require a confidence interval"
                )
            if self.candidate_wins + self.ties + self.candidate_losses != len(
                self.work_groups
            ):
                raise ValueError("win/tie/loss counts must cover every work group")
        elif any(value is not None for value in statistics_values) or (
            self.confidence_interval is not None
        ):
            raise ValueError("empty paired results cannot declare statistics")
        return self


class EvaluationAnalysisV3(FrozenAnalysisContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    run_id: str
    completion_status: Literal["partial", "complete"]
    bootstrap_method: Literal["work_group_percentile_bootstrap"] = (
        "work_group_percentile_bootstrap"
    )
    bootstrap_samples: int = Field(ge=100)
    random_seed: int = Field(ge=0, le=2**32 - 1)
    expected_observation_count: int = Field(ge=1)
    accepted_observation_count: int = Field(ge=0)
    missing_observation_ids: tuple[str, ...]
    issues: tuple[CompletionIssueV3, ...]
    comparisons: tuple[PairedMetricComparisonV3, ...]
    analysis_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_analysis(self) -> EvaluationAnalysisV3:
        if len(self.missing_observation_ids) != len(set(self.missing_observation_ids)):
            raise ValueError("missing observation identifiers must be unique")
        expected_comparisons = {
            (baseline_id, candidate_id, metric)
            for baseline_id, candidate_id in combinations(PACKAGE7_VARIANT_ORDER, 2)
            for metric in PACKAGE7_METRICS
        }
        actual_comparisons = {
            (
                item.baseline_variant_id,
                item.candidate_variant_id,
                item.metric,
            )
            for item in self.comparisons
        }
        if len(actual_comparisons) != len(self.comparisons):
            raise ValueError("analysis contains duplicate comparison cells")
        if actual_comparisons != expected_comparisons:
            raise ValueError("analysis must report every frozen v3 comparison cell")
        complete_matrix = (
            self.accepted_observation_count == self.expected_observation_count
            and not self.missing_observation_ids
            and not self.issues
        )
        if (self.completion_status == "complete") != complete_matrix:
            raise ValueError("completion status does not match the verified matrix")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"analysis_sha256"})
        )
        if self.analysis_sha256 != expected_hash:
            raise ValueError("analysis canonical hash mismatch")
        return self


def comparison_seed_v3(
    random_seed: int,
    baseline_variant_id: VariantIdV3,
    candidate_variant_id: VariantIdV3,
    metric: EvaluationMetricV3,
) -> int:
    """Derive a stable per-comparison bootstrap seed."""

    payload = (
        f"{random_seed}\0{baseline_variant_id}\0{candidate_variant_id}\0{metric.value}"
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big")


def compare_variants_v3(
    observations: Sequence[EvaluationObservationV3],
    *,
    baseline_variant_id: VariantIdV3,
    candidate_variant_id: VariantIdV3,
    metric: EvaluationMetricV3,
    bootstrap_samples: int,
    random_seed: int,
) -> PairedMetricComparisonV3:
    """Compare variants after nesting repetitions and paraphrases by work group."""

    if baseline_variant_id == candidate_variant_id:
        raise ValueError("paired comparison requires distinct variants")
    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")

    selected = tuple(
        item
        for item in observations
        if item.variant_id in {baseline_variant_id, candidate_variant_id}
    )
    indexed: dict[tuple[VariantIdV3, str, str, int], EvaluationObservationV3] = {}
    for item in selected:
        key = (item.variant_id, item.work_group_id, item.case_id, item.repetition)
        if key in indexed:
            raise ValueError(f"duplicate paired observation cell: {key}")
        indexed[key] = item

    baseline_index = {
        (item.work_group_id, item.case_id, item.repetition): item
        for item in selected
        if item.variant_id == baseline_variant_id
    }
    candidate_index = {
        (item.work_group_id, item.case_id, item.repetition): item
        for item in selected
        if item.variant_id == candidate_variant_id
    }
    paired_keys = sorted(set(baseline_index) & set(candidate_index))
    nested: dict[
        str,
        dict[str, list[tuple[float, float]]],
    ] = defaultdict(lambda: defaultdict(list))
    missing_value_ids: set[str] = set()
    for work_group_id, case_id, repetition in paired_keys:
        baseline = baseline_index[(work_group_id, case_id, repetition)]
        candidate = candidate_index[(work_group_id, case_id, repetition)]
        baseline_value = metric_value_v3(baseline, metric)
        candidate_value = metric_value_v3(candidate, metric)
        if baseline_value is None or candidate_value is None:
            if baseline_value is None:
                missing_value_ids.add(baseline.observation_id)
            if candidate_value is None:
                missing_value_ids.add(candidate.observation_id)
            continue
        nested[work_group_id][case_id].append((baseline_value, candidate_value))

    work_groups: list[WorkGroupDeltaV3] = []
    for work_group_id in sorted(nested):
        case_values = nested[work_group_id]
        baseline_case_means = [
            statistics.fmean(value[0] for value in case_values[case_id])
            for case_id in sorted(case_values)
        ]
        candidate_case_means = [
            statistics.fmean(value[1] for value in case_values[case_id])
            for case_id in sorted(case_values)
        ]
        baseline_mean = statistics.fmean(baseline_case_means)
        candidate_mean = statistics.fmean(candidate_case_means)
        work_groups.append(
            WorkGroupDeltaV3(
                work_group_id=work_group_id,
                conversation_count=len(case_values),
                paired_repetition_count=sum(
                    len(values) for values in case_values.values()
                ),
                baseline_mean=baseline_mean,
                candidate_mean=candidate_mean,
                delta=candidate_mean - baseline_mean,
            )
        )

    deltas = [item.delta for item in work_groups]
    direction = METRIC_DIRECTIONS_V3[metric]
    wins, ties, losses = _win_tie_loss(deltas, direction)
    confidence_interval = (
        _work_group_bootstrap_interval(
            deltas,
            bootstrap_samples=bootstrap_samples,
            random_seed=random_seed,
        )
        if deltas
        else None
    )
    return PairedMetricComparisonV3(
        baseline_variant_id=baseline_variant_id,
        candidate_variant_id=candidate_variant_id,
        metric=metric,
        direction=direction,
        paired_work_group_count=len(work_groups),
        paired_conversation_count=sum(item.conversation_count for item in work_groups),
        paired_observation_count=sum(
            item.paired_repetition_count for item in work_groups
        ),
        baseline_mean=(
            statistics.fmean(item.baseline_mean for item in work_groups)
            if work_groups
            else None
        ),
        candidate_mean=(
            statistics.fmean(item.candidate_mean for item in work_groups)
            if work_groups
            else None
        ),
        mean_delta=statistics.fmean(deltas) if deltas else None,
        median_delta=statistics.median(deltas) if deltas else None,
        candidate_wins=wins,
        ties=ties,
        candidate_losses=losses,
        confidence_interval=confidence_interval,
        work_groups=tuple(work_groups),
        missing_value_observation_ids=tuple(sorted(missing_value_ids)),
    )


def analyze_evaluation_v3(
    protocol: EvaluationProtocolV3,
    split: EvaluationSplitManifestV3,
    repeat_decision: RepeatDecisionV3,
    observations: Sequence[EvaluationObservationV3],
    *,
    run_id: str,
    gold_sha256: str,
    split_sha256: str,
    schedule_sha256: str,
    bootstrap_samples: int = 10_000,
    random_seed: int | None = None,
    invalid_ledger_observation_ids: Sequence[str] = (),
    stop: EvaluationStopV3 | None = None,
) -> EvaluationAnalysisV3:
    """Validate the held-out matrix and produce all grouped paired comparisons."""

    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    validate_evaluation_protocol_v3(protocol)
    protocol_hash = evaluation_protocol_sha256_v3(protocol)
    if protocol.assets.gold_sha256 != gold_sha256:
        raise ValueError("gold hash does not match the frozen protocol")
    if protocol.assets.split_sha256 != split_sha256:
        raise ValueError("split hash does not match the frozen protocol")
    if split.gold_sha256 != gold_sha256:
        raise ValueError("split manifest does not bind the supplied gold")
    if canonical_sha256(split) != split_sha256:
        raise ValueError("split canonical hash mismatch")
    if repeat_decision.protocol_sha256 != protocol_hash:
        raise ValueError("repeat decision protocol hash mismatch")
    if repeat_decision.repeat_rule_sha256 != protocol.repeat_rule_sha256:
        raise ValueError("repeat decision rule hash mismatch")

    bindings = ArtifactBindingsV3(
        protocol_sha256=protocol_hash,
        gold_sha256=gold_sha256,
        split_sha256=split_sha256,
        schedule_sha256=schedule_sha256,
        repeat_decision_sha256=canonical_sha256(repeat_decision),
        evaluator_configuration_sha256=(protocol.assets.evaluator_configuration_sha256),
        judge_prompt_sha256=protocol.assets.judge_prompt_sha256,
        judge_schema_sha256=protocol.assets.judge_schema_sha256,
        tool_contract_sha256=protocol.assets.tool_contract_sha256,
        corpus_sha256=protocol.assets.corpus_sha256,
        index_sha256=protocol.assets.index_sha256,
        embedding_model_dimensions_sha256=(
            protocol.assets.embedding_model_dimensions_sha256
        ),
        pricing_manifest_sha256=protocol.assets.pricing_manifest_sha256,
        usage_ledger_sha256=_usage_ledger_evidence_sha256(observations),
        rubric_sha256=protocol.assets.rubric_sha256,
    )
    heldout = tuple(
        entry for entry in split.entries if entry.split == EvaluationSplitV3.HELD_OUT
    )
    expected_cells: dict[tuple[VariantIdV3, str, int], str] = {}
    expected_work_groups: dict[str, str] = {}
    for entry in heldout:
        if entry.conversation_id in expected_work_groups:
            raise ValueError("held-out split contains duplicate conversation IDs")
        expected_work_groups[entry.conversation_id] = entry.work_group_id
        for variant_id in PACKAGE7_VARIANT_ORDER:
            for repetition in range(repeat_decision.selected_repeats):
                identity = ObservationIdentityV3(
                    run_id=run_id,
                    protocol_sha256=protocol_hash,
                    schedule_algorithm_id=protocol.schedule_algorithm_id,
                    turn_kind=ScheduledTurnKindV3.MEASURED,
                    variant_id=variant_id,
                    case_id=entry.conversation_id,
                    work_group_id=entry.work_group_id,
                    repetition=repetition,
                )
                key = (variant_id, entry.conversation_id, repetition)
                expected_cells[key] = canonical_observation_id_v3(identity)

    issues: list[CompletionIssueV3] = []
    accepted: list[EvaluationObservationV3] = []
    accepted_cells: set[tuple[VariantIdV3, str, int]] = set()
    execution_orders: set[int] = set()
    ambiguous_identifiers: list[str] = []
    for observation in observations:
        if observation.run_id != run_id:
            raise ValueError("observation run ID does not match the analysis run")
        if observation.protocol_sha256 != protocol_hash:
            raise ValueError("observation protocol hash does not match the analysis")
        if observation.identity.schedule_algorithm_id != protocol.schedule_algorithm_id:
            raise ValueError("observation schedule algorithm is foreign")
        key = (
            observation.variant_id,
            observation.case_id,
            observation.repetition,
        )
        if key not in expected_cells:
            raise ValueError(f"foreign observation cell: {key}")
        if key in accepted_cells:
            raise ValueError(f"duplicate observation cell: {key}")
        if observation.execution_order in execution_orders:
            raise ValueError("observation execution orders must be unique")
        accepted_cells.add(key)
        execution_orders.add(observation.execution_order)
        expected_work_group = expected_work_groups[observation.case_id]
        if observation.work_group_id != expected_work_group:
            ambiguous_identifiers.extend(
                (
                    f"observation_id={observation.observation_id}",
                    f"case_id={observation.case_id}",
                    f"expected_work_group_id={expected_work_group}",
                    f"observed_work_group_id={observation.work_group_id}",
                )
            )
            continue
        if observation.observation_id != expected_cells[key]:
            raise ValueError("observation canonical identity does not match the split")
        accepted.append(observation)

    if ambiguous_identifiers:
        issues.append(
            CompletionIssueV3(
                code="ambiguous_work_group",
                identifiers=tuple(ambiguous_identifiers),
                message="observed work-group identity conflicts with the frozen split",
            )
        )

    unknown_invalid_ids = set(invalid_ledger_observation_ids) - {
        item.observation_id for item in observations
    }
    if unknown_invalid_ids:
        raise ValueError(
            "invalid-ledger identifiers are not present in observations: "
            f"{sorted(unknown_invalid_ids)}"
        )
    invalid_ledger_ids = set(invalid_ledger_observation_ids)
    ledger_owners: dict[str, list[str]] = defaultdict(list)
    for observation in observations:
        for ledger_event_id in observation.ledger_event_ids:
            ledger_owners[ledger_event_id].append(observation.observation_id)
    reused_events = {
        event_id: owner_ids
        for event_id, owner_ids in ledger_owners.items()
        if len(owner_ids) > 1
    }
    if reused_events:
        invalid_ledger_ids.update(
            owner_id for owner_ids in reused_events.values() for owner_id in owner_ids
        )
    if invalid_ledger_ids:
        identifiers = [
            f"observation_id={observation_id}"
            for observation_id in sorted(invalid_ledger_ids)
        ]
        identifiers.extend(
            f"ledger_event_id={event_id}" for event_id in sorted(reused_events)
        )
        issues.append(
            CompletionIssueV3(
                code="invalid_ledger",
                identifiers=tuple(identifiers),
                message="ledger evidence is invalid or reused across observations",
            )
        )

    accepted = [
        item for item in accepted if item.observation_id not in invalid_ledger_ids
    ]
    accepted_ids = {item.observation_id for item in accepted}

    missing_ids = tuple(sorted(set(expected_cells.values()) - accepted_ids))
    if missing_ids:
        issues.append(
            CompletionIssueV3(
                code="missing_cells",
                identifiers=missing_ids,
                message="held-out variant-by-conversation-by-repeat cells are missing",
            )
        )
    if stop is not None:
        issues.append(
            CompletionIssueV3(
                code=stop.code,
                identifiers=stop.affected_identifiers,
                message=stop.message,
            )
        )

    base_seed = protocol.random_seed if random_seed is None else random_seed
    comparisons_v3 = tuple(
        compare_variants_v3(
            accepted,
            baseline_variant_id=baseline_id,
            candidate_variant_id=candidate_id,
            metric=metric,
            bootstrap_samples=bootstrap_samples,
            random_seed=comparison_seed_v3(
                base_seed,
                baseline_id,
                candidate_id,
                metric,
            ),
        )
        for baseline_id, candidate_id in combinations(PACKAGE7_VARIANT_ORDER, 2)
        for metric in PACKAGE7_METRICS
    )
    payload = {
        "schema_version": "3.0",
        "bindings": bindings.model_dump(mode="json"),
        "run_id": run_id,
        "completion_status": "complete" if not issues else "partial",
        "bootstrap_method": "work_group_percentile_bootstrap",
        "bootstrap_samples": bootstrap_samples,
        "random_seed": base_seed,
        "expected_observation_count": len(expected_cells),
        "accepted_observation_count": len(accepted),
        "missing_observation_ids": missing_ids,
        "issues": [item.model_dump(mode="json") for item in issues],
        "comparisons": [item.model_dump(mode="json") for item in comparisons_v3],
    }
    return EvaluationAnalysisV3(
        bindings=bindings,
        run_id=run_id,
        completion_status="complete" if not issues else "partial",
        bootstrap_samples=bootstrap_samples,
        random_seed=base_seed,
        expected_observation_count=len(expected_cells),
        accepted_observation_count=len(accepted),
        missing_observation_ids=missing_ids,
        issues=tuple(issues),
        comparisons=comparisons_v3,
        analysis_sha256=canonical_sha256(payload),
    )


def metric_value_v3(
    observation: EvaluationObservationV3,
    metric: EvaluationMetricV3,
) -> float | None:
    if metric == EvaluationMetricV3.TASK_COMPLETION:
        return float(observation.task_completed)
    if metric == EvaluationMetricV3.ANSWERABILITY_ABSTENTION:
        return float(observation.abstained == (not observation.answerable))
    if metric == EvaluationMetricV3.CLAIM_SUPPORT:
        return observation.claim_support
    if metric == EvaluationMetricV3.CITATION_PRECISION:
        return observation.citation_precision
    if metric == EvaluationMetricV3.CITATION_COVERAGE:
        return observation.citation_coverage
    if metric == EvaluationMetricV3.DOCUMENT_RECALL:
        return observation.document_recall
    if metric == EvaluationMetricV3.AUTHORIZATION:
        return float(observation.authorized)
    if metric == EvaluationMetricV3.VALID_PLAN:
        return float(observation.valid_plan)
    if metric == EvaluationMetricV3.USEFUL_CONTINUATION:
        return (
            None
            if observation.useful_continuation is None
            else float(observation.useful_continuation)
        )
    if metric == EvaluationMetricV3.DUPLICATE_DISPATCH:
        return float(observation.duplicate_dispatch_count)
    if metric == EvaluationMetricV3.END_TO_END_LATENCY_MS:
        return observation.end_to_end_latency_ms
    if metric == EvaluationMetricV3.TOTAL_TOKENS:
        return float(observation.total_tokens)
    if metric == EvaluationMetricV3.EFFECTIVE_COST_USD:
        return float(observation.effective_cost_usd)
    raise ValueError(f"unsupported Evaluation v3 metric: {metric}")


def _usage_ledger_evidence_sha256(
    observations: Sequence[EvaluationObservationV3],
) -> str:
    """Bind the exact ledger attribution used by this analysis capture."""

    return canonical_sha256(
        [
            {
                "observation_id": item.observation_id,
                "ledger_event_ids": item.ledger_event_ids,
                "known_cost_usd": str(item.known_cost_usd),
                "unresolved_reserved_cost_usd": str(item.unresolved_reserved_cost_usd),
            }
            for item in sorted(observations, key=lambda item: item.observation_id)
        ]
    )


def _work_group_bootstrap_interval(
    deltas: Sequence[float],
    *,
    bootstrap_samples: int,
    random_seed: int,
) -> ConfidenceIntervalV3:
    generator = random.Random(random_seed)
    sample_size = len(deltas)
    means = sorted(
        statistics.fmean(generator.choice(deltas) for _ in range(sample_size))
        for _ in range(bootstrap_samples)
    )
    return ConfidenceIntervalV3(
        lower=_percentile(means, 0.025),
        upper=_percentile(means, 0.975),
        bootstrap_samples=bootstrap_samples,
        random_seed=random_seed,
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    position = (len(values) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return values[lower_index]
    weight = position - lower_index
    return values[lower_index] * (1 - weight) + values[upper_index] * weight


def _win_tie_loss(
    deltas: Sequence[float],
    direction: MetricDirectionV3,
) -> tuple[int, int, int]:
    wins = ties = losses = 0
    for delta in deltas:
        directed = delta if direction == "higher_is_better" else -delta
        if math.isclose(directed, 0.0, abs_tol=_TIE_TOLERANCE):
            ties += 1
        elif directed > 0:
            wins += 1
        else:
            losses += 1
    return wins, ties, losses
