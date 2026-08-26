"""Strict contracts for reproducible paired and robustness evaluation."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts import TaskStatus
from app.evaluation.models import EvalCase

_IDENTIFIER = r"^[a-z][a-z0-9_.-]{2,127}$"
_SHA256 = r"^[a-f0-9]{64}$"


class RuntimeMode(StrEnum):
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    CAPTURED = "captured"


class ModelStage(StrEnum):
    ROUTING = "routing"
    PLANNING = "planning"
    SPECIALIST = "specialist"
    SYNTHESIS = "synthesis"
    JUDGE = "judge"


class ModelCallOutcome(StrEnum):
    SUCCESS = "success"
    FALLBACK = "fallback"
    ERROR = "error"
    REFUSAL = "refusal"


class ComparisonMetric(StrEnum):
    TASK_SUCCESS = "task_success"
    ROUTING_CORRECT = "routing_correct"
    EXACT_PLAN = "exact_plan"
    ANSWER_ASSERTION_ACCURACY = "answer_assertion_accuracy"
    RETRIEVAL_F1 = "retrieval_f1"
    END_TO_END_LATENCY_MS = "end_to_end_latency_ms"
    TOTAL_TOKENS = "total_tokens"
    ESTIMATED_COST_USD = "estimated_cost_usd"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class RobustnessPolicy(StrEnum):
    CLEAN = "clean"
    LABEL_PRESERVING = "label_preserving"
    STRESS = "stress"


class EvaluationPhase(StrEnum):
    CORRECTNESS = "correctness"
    LATENCY = "latency"


class ReasoningEffortV2(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ModelBindingV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: ModelStage
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: ReasoningEffortV2 = ReasoningEffortV2.LOW


class ModelRuntimePolicyV2(BaseModel):
    """Non-secret provider controls frozen into an executable protocol."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai"] = "openai"
    request_timeout_seconds: float = Field(ge=1, le=120)
    max_retries: int = Field(ge=0, le=5)
    max_output_tokens: int = Field(ge=64, le=16_384)
    max_concurrency: int = Field(ge=1, le=64)
    circuit_failure_threshold: int = Field(ge=1, le=20)
    circuit_recovery_seconds: float = Field(ge=1, le=600)


class EvaluationVariantV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variant_id: str = Field(pattern=_IDENTIFIER)
    description: str = Field(min_length=1, max_length=1_000)
    runtime_mode: RuntimeMode
    model_router_enabled: bool = False
    model_planner_enabled: bool = False
    model_specialists_enabled: bool = False
    model_synthesis_enabled: bool = False
    enabled_agents: tuple[str, ...] = Field(min_length=1)
    embedding_backend: Literal["hashed_token_cosine_v1"]
    model_bindings: tuple[ModelBindingV2, ...] = ()
    max_model_calls: int = Field(default=0, ge=0, le=100)
    fallback_policy: Literal["deterministic_fallback", "fail_closed"] = (
        "deterministic_fallback"
    )
    parent_variant_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    hypothesis: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_variant(self) -> EvaluationVariantV2:
        if len(self.enabled_agents) != len(set(self.enabled_agents)):
            raise ValueError("enabled agents must be unique")
        model_stages_enabled = any(
            (
                self.model_router_enabled,
                self.model_planner_enabled,
                self.model_specialists_enabled,
                self.model_synthesis_enabled,
            )
        )
        if self.runtime_mode == RuntimeMode.DETERMINISTIC and (
            model_stages_enabled or self.model_bindings or self.max_model_calls
        ):
            raise ValueError("deterministic variants cannot declare model calls")
        if self.runtime_mode == RuntimeMode.HYBRID and not model_stages_enabled:
            raise ValueError("hybrid variants must enable at least one model stage")
        if model_stages_enabled and self.max_model_calls < 1:
            raise ValueError("model-enabled variants require max_model_calls")
        enabled_stages = {
            stage
            for stage, enabled in (
                (ModelStage.ROUTING, self.model_router_enabled),
                (ModelStage.PLANNING, self.model_planner_enabled),
                (ModelStage.SPECIALIST, self.model_specialists_enabled),
                (ModelStage.SYNTHESIS, self.model_synthesis_enabled),
            )
            if enabled
        }
        bound_stages = [binding.stage for binding in self.model_bindings]
        if ModelStage.JUDGE in bound_stages:
            raise ValueError("judge models are evaluator configuration, not a variant")
        if len(bound_stages) != len(set(bound_stages)):
            raise ValueError("model bindings must use unique stages")
        if set(bound_stages) != enabled_stages:
            raise ValueError("model bindings must exactly match enabled model stages")
        if self.parent_variant_id == self.variant_id:
            raise ValueError("a variant cannot be its own parent")
        return self


class EvaluationProtocolV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = "2.0"
    protocol_id: str = Field(pattern=_IDENTIFIER)
    experiment_sha256: str = Field(pattern=_SHA256)
    baseline_variant_id: str = Field(pattern=_IDENTIFIER)
    corpus_id: str = Field(pattern=_IDENTIFIER)
    corpus_sha256: str = Field(pattern=_SHA256)
    dataset_id: str = Field(pattern=_IDENTIFIER)
    dataset_sha256: str = Field(pattern=_SHA256)
    sample_seed_sha256: str = Field(pattern=_SHA256)
    evaluator_sha256: str = Field(pattern=_SHA256)
    sample_data: Literal[True] = True
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)
    correctness_repeats: int = Field(default=1, ge=0, le=50)
    warmup_repeats: int = Field(default=0, ge=0, le=20)
    latency_repeats: int = Field(default=0, ge=0, le=100)
    bootstrap_samples: int = Field(default=5_000, ge=100, le=100_000)
    case_order: tuple[str, ...] = Field(min_length=1, max_length=5_000)
    latency_case_order: tuple[str, ...] = Field(default=(), max_length=5_000)
    warmup_case_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    variants: tuple[EvaluationVariantV2, ...] = Field(min_length=1, max_length=50)
    model_runtime_policy: ModelRuntimePolicyV2 | None = None
    pricing_sha256: str | None = Field(default=None, pattern=_SHA256)
    judge_prompt_sha256: str | None = Field(default=None, pattern=_SHA256)
    judge_schema_sha256: str | None = Field(default=None, pattern=_SHA256)
    git_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    git_dirty: bool
    network_allowed: bool = False

    @model_validator(mode="after")
    def validate_protocol(self) -> EvaluationProtocolV2:
        if self.correctness_repeats + self.latency_repeats < 1:
            raise ValueError("protocol requires correctness or latency observations")
        if len(self.case_order) != len(set(self.case_order)):
            raise ValueError("case order must contain unique case IDs")
        if len(self.latency_case_order) != len(set(self.latency_case_order)):
            raise ValueError("latency case order must contain unique case IDs")
        if not set(self.latency_case_order).issubset(self.case_order):
            raise ValueError("latency cases must be a subset of the corpus cases")
        if (self.warmup_repeats > 0) != (self.warmup_case_id is not None):
            raise ValueError(
                "warmup repeats and pinned warmup case must be declared together"
            )
        if (
            self.warmup_case_id is not None
            and self.warmup_case_id not in self.case_order
        ):
            raise ValueError("warmup case must belong to the protocol case order")
        variant_ids = [variant.variant_id for variant in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("variant IDs must be unique")
        known_variants = set(variant_ids)
        if self.baseline_variant_id not in known_variants:
            raise ValueError("protocol baseline variant is unknown")
        for variant in self.variants:
            if (
                variant.parent_variant_id is not None
                and variant.parent_variant_id not in known_variants
            ):
                raise ValueError(f"unknown parent variant: {variant.parent_variant_id}")
        _validate_variant_parent_graph(self.variants)
        requires_network = any(
            variant.runtime_mode == RuntimeMode.HYBRID for variant in self.variants
        )
        if self.network_allowed != requires_network:
            raise ValueError(
                "protocol network policy must exactly match executable variants"
            )
        if requires_network != (self.model_runtime_policy is not None):
            raise ValueError(
                "hybrid protocols must declare exactly one model runtime policy"
            )
        if self.model_runtime_policy is not None:
            for variant in self.variants:
                if variant.runtime_mode != RuntimeMode.HYBRID:
                    continue
                if any(
                    binding.provider != self.model_runtime_policy.provider
                    for binding in variant.model_bindings
                ):
                    raise ValueError(
                        "hybrid model binding provider must match runtime policy"
                    )
        return self

    @property
    def effective_latency_case_order(self) -> tuple[str, ...]:
        return self.latency_case_order or self.case_order


class EvaluationExperimentConfigV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = "2.0"
    experiment_id: str = Field(pattern=_IDENTIFIER)
    baseline_variant_id: str = Field(pattern=_IDENTIFIER)
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)
    correctness_repeats: int = Field(default=3, ge=1, le=50)
    warmup_repeats: int = Field(default=1, ge=0, le=20)
    latency_repeats: int = Field(default=5, ge=0, le=100)
    bootstrap_samples: int = Field(default=10_000, ge=100, le=100_000)
    warmup_case_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    latency_case_ids: tuple[str, ...] = Field(default=(), max_length=5_000)
    variants: tuple[EvaluationVariantV2, ...] = Field(min_length=2, max_length=50)
    model_runtime_policy: ModelRuntimePolicyV2 | None = None

    @model_validator(mode="after")
    def validate_experiment(self) -> EvaluationExperimentConfigV2:
        variant_ids = [variant.variant_id for variant in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("experiment variant IDs must be unique")
        if self.baseline_variant_id not in variant_ids:
            raise ValueError("experiment baseline variant is unknown")
        known_variants = set(variant_ids)
        for variant in self.variants:
            if (
                variant.parent_variant_id is not None
                and variant.parent_variant_id not in known_variants
            ):
                raise ValueError(
                    "experiment contains unknown parent variant: "
                    f"{variant.parent_variant_id}"
                )
        _validate_variant_parent_graph(self.variants)
        if len(self.latency_case_ids) != len(set(self.latency_case_ids)):
            raise ValueError("experiment latency case IDs must be unique")
        if (self.warmup_repeats > 0) != (self.warmup_case_id is not None):
            raise ValueError(
                "experiment warmup repeats and pinned case must be declared together"
            )
        hybrid_variants = tuple(
            variant
            for variant in self.variants
            if variant.runtime_mode == RuntimeMode.HYBRID
        )
        if bool(hybrid_variants) != (self.model_runtime_policy is not None):
            raise ValueError(
                "hybrid experiments must declare exactly one model runtime policy"
            )
        if self.model_runtime_policy is not None and any(
            binding.provider != self.model_runtime_policy.provider
            for variant in hybrid_variants
            for binding in variant.model_bindings
        ):
            raise ValueError("hybrid model binding provider must match runtime policy")
        return self


class RobustnessCaseDefinitionV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=_IDENTIFIER)
    parent_case_id: str = Field(pattern=_IDENTIFIER)
    transform_id: str = Field(pattern=_IDENTIFIER)
    policy: RobustnessPolicy
    message: str = Field(min_length=2, max_length=2_000)
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_transform(self) -> RobustnessCaseDefinitionV2:
        if self.case_id == self.parent_case_id:
            raise ValueError("robustness case must differ from its parent")
        if self.policy == RobustnessPolicy.CLEAN:
            raise ValueError("robustness case cannot use the clean policy")
        return self


class EvaluationCorpusManifestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = "2.0"
    corpus_id: str = Field(pattern=_IDENTIFIER)
    base_dataset_id: str = Field(pattern=_IDENTIFIER)
    base_dataset_sha256: str = Field(pattern=_SHA256)
    sample_data: Literal[True] = True
    curation_note: str = Field(min_length=1, max_length=2_000)
    robustness_cases: tuple[RobustnessCaseDefinitionV2, ...] = Field(
        min_length=1,
        max_length=500,
    )

    @model_validator(mode="after")
    def validate_cases(self) -> EvaluationCorpusManifestV2:
        case_ids = [case.case_id for case in self.robustness_cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("robustness case IDs must be unique")
        transform_keys = [
            (case.parent_case_id, case.transform_id) for case in self.robustness_cases
        ]
        if len(transform_keys) != len(set(transform_keys)):
            raise ValueError("each parent can use a transform only once")
        return self


class EvaluationCaseSpecV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=_IDENTIFIER)
    gold_case: EvalCase
    robustness_policy: RobustnessPolicy
    parent_case_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    transform_id: str | None = Field(default=None, pattern=_IDENTIFIER)

    @model_validator(mode="after")
    def validate_case_identity(self) -> EvaluationCaseSpecV2:
        if self.gold_case.case_id != self.case_id:
            raise ValueError("gold case ID must match executable case ID")
        if self.robustness_policy == RobustnessPolicy.CLEAN:
            if self.parent_case_id is not None or self.transform_id is not None:
                raise ValueError("clean executable cases cannot reference a parent")
        elif self.parent_case_id is None or self.transform_id is None:
            raise ValueError("transformed cases require parent and transform IDs")
        return self


class TokenUsageV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_token_accounting(self) -> TokenUsageV2:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens must be included in output tokens")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total tokens must equal input plus output tokens")
        return self


class ModelCallV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    call_id: str = Field(pattern=_IDENTIFIER)
    stage: ModelStage
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    model: str = Field(min_length=1, max_length=128)
    outcome: ModelCallOutcome
    attempt: int = Field(default=1, ge=0, le=20)
    latency_ms: float = Field(ge=0)
    usage: TokenUsageV2 | None = None
    response_id: str | None = Field(default=None, min_length=1, max_length=256)
    error_code: str | None = Field(default=None, pattern=_IDENTIFIER)
    fallback_reason: str | None = Field(default=None, pattern=_IDENTIFIER)

    @model_validator(mode="after")
    def validate_outcome(self) -> ModelCallV2:
        if self.outcome == ModelCallOutcome.ERROR and self.error_code is None:
            raise ValueError("failed model calls require an error code")
        if self.outcome == ModelCallOutcome.SUCCESS and self.error_code is not None:
            raise ValueError("successful model calls cannot carry an error code")
        if self.attempt == 0 and self.error_code != "model_circuit_open":
            raise ValueError("zero-attempt calls must be rejected by an open circuit")
        if self.error_code == "model_circuit_open" and self.attempt != 0:
            raise ValueError("open-circuit calls cannot declare provider attempts")
        if (self.outcome == ModelCallOutcome.FALLBACK) != (
            self.fallback_reason is not None
        ):
            raise ValueError(
                "fallback outcome and fallback reason must be declared together"
            )
        return self


def _validate_variant_parent_graph(
    variants: tuple[EvaluationVariantV2, ...],
) -> None:
    parents = {variant.variant_id: variant.parent_variant_id for variant in variants}
    for variant_id in parents:
        visited: set[str] = set()
        current: str | None = variant_id
        while current is not None:
            if current in visited:
                raise ValueError("variant parent graph must be acyclic")
            visited.add(current)
            current = parents.get(current)


class PlanEdgeV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_step_id: str = Field(pattern=_IDENTIFIER)
    target_step_id: str = Field(pattern=_IDENTIFIER)


class EvaluationObservationV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["2.0"] = "2.0"
    run_id: str = Field(pattern=_IDENTIFIER)
    protocol_sha256: str = Field(pattern=_SHA256)
    variant_id: str = Field(pattern=_IDENTIFIER)
    case_id: str = Field(pattern=_IDENTIFIER)
    category: str = Field(pattern=_IDENTIFIER)
    phase: EvaluationPhase
    repetition: int = Field(ge=0)
    execution_order: int = Field(ge=0)
    random_seed: int = Field(ge=0, le=2**32 - 1)
    robustness_policy: RobustnessPolicy = RobustnessPolicy.CLEAN
    parent_case_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    transform_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    status: TaskStatus
    predicted_intent: str = Field(min_length=1, max_length=128)
    actions: tuple[str, ...] = ()
    dependency_edges: tuple[PlanEdgeV2, ...] = ()
    retrieved_product_ids: tuple[int, ...] = ()
    selected_product_id: int | None = Field(default=None, ge=1)
    provenance_source_ids: tuple[str, ...] = ()
    answer: str = Field(max_length=50_000)
    assertions_passed: int = Field(ge=0)
    assertion_count: int = Field(ge=0)
    routing_correct: bool
    exact_plan: bool
    task_success: bool
    refusal_or_incomplete: bool = False
    end_to_end_latency_ms: float = Field(ge=0)
    routing_latency_ms: float | None = Field(default=None, ge=0)
    planning_latency_ms: float | None = Field(default=None, ge=0)
    agent_latency_ms: float | None = Field(default=None, ge=0)
    synthesis_latency_ms: float | None = Field(default=None, ge=0)
    retrieval_f1: float | None = Field(default=None, ge=0, le=1)
    model_calls: tuple[ModelCallV2, ...] = ()
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    cost_unavailable_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=2_000,
    )
    error_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_observation(self) -> EvaluationObservationV2:
        if self.assertions_passed > self.assertion_count:
            raise ValueError("passed assertions cannot exceed assertion count")
        if len(self.retrieved_product_ids) != len(set(self.retrieved_product_ids)):
            raise ValueError("retrieved product IDs must be unique and ranked")
        if any(product_id < 1 for product_id in self.retrieved_product_ids):
            raise ValueError("retrieved product IDs must be positive")
        if len(self.provenance_source_ids) != len(set(self.provenance_source_ids)):
            raise ValueError("provenance source IDs must be unique")
        if (
            self.estimated_cost_usd is not None
            and self.cost_unavailable_reason is not None
        ):
            raise ValueError("available cost cannot carry an unavailable reason")
        call_ids = [call.call_id for call in self.model_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("model call IDs must be unique per observation")
        if self.robustness_policy == RobustnessPolicy.CLEAN:
            if self.parent_case_id is not None or self.transform_id is not None:
                raise ValueError("clean cases cannot reference a robustness parent")
        elif self.parent_case_id is None or self.transform_id is None:
            raise ValueError("robustness variants require parent and transform IDs")
        stage_latencies = (
            self.routing_latency_ms,
            self.planning_latency_ms,
            self.agent_latency_ms,
            self.synthesis_latency_ms,
        )
        if any(
            latency is not None and latency > self.end_to_end_latency_ms
            for latency in stage_latencies
        ):
            raise ValueError(
                "individual stage latency cannot exceed end-to-end latency"
            )
        return self

    @property
    def answer_assertion_accuracy(self) -> float | None:
        if self.assertion_count == 0:
            return None
        return self.assertions_passed / self.assertion_count

    @property
    def total_tokens(self) -> int:
        return sum(call.usage.total_tokens for call in self.model_calls if call.usage)


class ModelPriceV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1, max_length=128)
    input_per_million_usd: Decimal = Field(ge=0)
    cached_input_per_million_usd: Decimal = Field(ge=0)
    output_per_million_usd: Decimal = Field(ge=0)


class PricingManifestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = "2.0"
    pricing_id: str = Field(pattern=_IDENTIFIER)
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    effective_at: date
    currency: Literal["USD"] = "USD"
    source_url: str = Field(pattern=r"^https://")
    entries: tuple[ModelPriceV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_models(self) -> PricingManifestV2:
        models = [entry.model for entry in self.entries]
        if len(models) != len(set(models)):
            raise ValueError("pricing entries must use unique model IDs")
        return self


class CostEstimateV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value_usd: Decimal | None = Field(default=None, ge=0)
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def explain_availability(self) -> CostEstimateV2:
        if (self.value_usd is None) == (self.unavailable_reason is None):
            raise ValueError(
                "cost must have exactly one of value or unavailable reason"
            )
        return self


class ConfidenceIntervalV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    confidence: float = Field(default=0.95, ge=0.95, le=0.95)
    lower: float
    upper: float
    bootstrap_samples: int = Field(ge=100)
    random_seed: int = Field(ge=0, le=2**32 - 1)

    @model_validator(mode="after")
    def validate_bounds(self) -> ConfidenceIntervalV2:
        if self.lower > self.upper:
            raise ValueError("confidence interval lower bound exceeds upper bound")
        return self


class WinTieLossV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    losses: int = Field(ge=0)


class PairedComparisonV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["2.0"] = "2.0"
    protocol_sha256: str = Field(pattern=_SHA256)
    baseline_variant_id: str = Field(pattern=_IDENTIFIER)
    candidate_variant_id: str = Field(pattern=_IDENTIFIER)
    metric: ComparisonMetric
    phase: EvaluationPhase
    direction: MetricDirection
    observation_pair_count: int = Field(ge=0)
    case_count: int = Field(ge=0)
    excluded_pair_count: int = Field(ge=0)
    baseline_mean: float
    candidate_mean: float
    mean_delta: float
    median_delta: float
    confidence_interval: ConfidenceIntervalV2
    win_tie_loss: WinTieLossV2
    paired_effect_size: float | None = None
    exact_two_sided_p_value: float | None = Field(default=None, ge=0, le=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_comparison(self) -> PairedComparisonV2:
        if self.baseline_variant_id == self.candidate_variant_id:
            raise ValueError("paired comparison requires distinct variants")
        decisions = (
            self.win_tie_loss.wins + self.win_tie_loss.ties + self.win_tie_loss.losses
        )
        if decisions != self.case_count:
            raise ValueError("win/tie/loss counts must equal compared case count")
        if self.observation_pair_count < self.case_count:
            raise ValueError("observation pairs cannot be fewer than compared cases")
        if not math.isclose(
            self.mean_delta,
            self.candidate_mean - self.baseline_mean,
            abs_tol=1e-12,
        ):
            raise ValueError("mean delta must equal candidate mean minus baseline mean")
        return self


class ComparisonOmissionV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = "2.0"
    protocol_sha256: str = Field(pattern=_SHA256)
    baseline_variant_id: str = Field(pattern=_IDENTIFIER)
    candidate_variant_id: str = Field(pattern=_IDENTIFIER)
    metric: ComparisonMetric
    phase: EvaluationPhase
    reason: Literal["no_comparable_paired_values"]

    @model_validator(mode="after")
    def validate_pair(self) -> ComparisonOmissionV2:
        if self.baseline_variant_id == self.candidate_variant_id:
            raise ValueError("omitted comparison requires distinct variants")
        return self


class RobustnessTransformSummaryV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    transform_id: str = Field(pattern=_IDENTIFIER)
    case_count: int = Field(ge=1)
    task_success_rate: float = Field(ge=0, le=1)
    intent_consistency_rate: float = Field(ge=0, le=1)
    mean_task_success_degradation: float = Field(ge=-1, le=1)


class RobustnessSummaryV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["2.0"] = "2.0"
    protocol_sha256: str = Field(pattern=_SHA256)
    variant_id: str = Field(pattern=_IDENTIFIER)
    clean_case_count: int = Field(ge=1)
    transformed_case_count: int = Field(ge=1)
    task_success_rate: float = Field(ge=0, le=1)
    intent_consistency_rate: float = Field(ge=0, le=1)
    mean_task_success_degradation: float = Field(ge=-1, le=1)
    maximum_task_success_degradation: float = Field(ge=-1, le=1)
    worst_transform_id: str = Field(pattern=_IDENTIFIER)
    transforms: tuple[RobustnessTransformSummaryV2, ...] = Field(min_length=1)


class ArtifactFileV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,127}$")
    sha256: str = Field(pattern=_SHA256)
    size_bytes: int = Field(ge=0)
    record_count: int | None = Field(default=None, ge=0)


class EvaluationBundleManifestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = "2.0"
    run_id: str = Field(pattern=_IDENTIFIER)
    protocol_sha256: str = Field(pattern=_SHA256)
    created_at: datetime
    completion_status: Literal["complete"] = "complete"
    git_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    git_dirty: bool
    observation_count: int = Field(ge=0)
    comparison_count: int = Field(ge=0)
    omission_count: int = Field(ge=0)
    files: tuple[ArtifactFileV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_files(self) -> EvaluationBundleManifestV2:
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("bundle file paths must be unique")
        return self
