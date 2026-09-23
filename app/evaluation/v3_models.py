"""Strict additive contracts for the Package 7 evaluation v3 protocol."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts import TaskStatus
from app.evaluation.protocol import canonical_sha256

_IDENTIFIER = r"^[a-z][a-z0-9_.-]{2,127}$"
_SHA256 = r"^[a-f0-9]{64}$"

IdentifierV3 = Annotated[str, Field(pattern=_IDENTIFIER)]
Sha256V3 = Annotated[str, Field(pattern=_SHA256)]
CaseIdV3 = Annotated[str, Field(pattern=_IDENTIFIER)]

VariantIdV3 = Literal[
    "sa_shared_tools_rag",
    "ma_fixed_rag",
    "ma_adaptive_rag",
    "ma_adaptive_no_rag",
]
PilotCaseIdV3 = Literal[
    "dev_multi_constraint_01",
    "dev_multi_constraint_02",
    "dev_knowledge_source_01",
    "dev_knowledge_source_02",
    "dev_multi_expert_01",
    "dev_multi_turn_memory_01",
    "dev_insufficient_conflict_injection_01",
    "dev_shopping_merchant_01",
]

PACKAGE7_VARIANT_ORDER: tuple[VariantIdV3, ...] = (
    "sa_shared_tools_rag",
    "ma_fixed_rag",
    "ma_adaptive_rag",
    "ma_adaptive_no_rag",
)
PACKAGE7_PILOT_CASE_ORDER: tuple[PilotCaseIdV3, ...] = (
    "dev_multi_constraint_01",
    "dev_multi_constraint_02",
    "dev_knowledge_source_01",
    "dev_knowledge_source_02",
    "dev_multi_expert_01",
    "dev_multi_turn_memory_01",
    "dev_insufficient_conflict_injection_01",
    "dev_shopping_merchant_01",
)


class FrozenContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EvaluationMetricV3(StrEnum):
    TASK_COMPLETION = "task_completion"
    ANSWERABILITY_ABSTENTION = "answerability_abstention"
    CLAIM_SUPPORT = "claim_support"
    CITATION_PRECISION = "citation_precision"
    CITATION_COVERAGE = "citation_coverage"
    DOCUMENT_RECALL = "document_recall"
    AUTHORIZATION = "authorization"
    VALID_PLAN = "valid_plan"
    USEFUL_CONTINUATION = "useful_continuation"
    DUPLICATE_DISPATCH = "duplicate_dispatch"
    END_TO_END_LATENCY_MS = "end_to_end_latency_ms"
    TOTAL_TOKENS = "total_tokens"
    EFFECTIVE_COST_USD = "effective_cost_usd"


PACKAGE7_METRICS: tuple[EvaluationMetricV3, ...] = tuple(EvaluationMetricV3)


class GoldAuthorshipV3(StrEnum):
    AUTOMATED_PRE_SUT_SPEC = "automated_pre_sut_spec"
    HUMAN_PRE_SUT_SPEC = "human_pre_sut_spec"


class JudgmentModeV3(StrEnum):
    DETERMINISTIC_RUBRIC = "deterministic_rubric"
    MODEL_JUDGE = "model_judge"
    HUMAN_REVIEW = "human_review"


class AgentTopologyV3(StrEnum):
    SINGLE = "single"
    MULTI = "multi"


class PlanningModeV3(StrEnum):
    SHARED = "shared"
    FIXED = "fixed"
    ADAPTIVE = "adaptive"


class ScheduledTurnKindV3(StrEnum):
    WARMUP = "warmup"
    MEASURED = "measured"


class GenerationBindingV3(FrozenContractV3):
    provider: Literal["openai"] = "openai"
    model: Literal["gpt-5.4-mini-2026-03-17"]
    reasoning_effort: Literal["low"] = "low"


class GoldPolicyV3(FrozenContractV3):
    authorship: GoldAuthorshipV3
    human_author_ids: tuple[IdentifierV3, ...] = ()

    @model_validator(mode="after")
    def validate_authorship(self) -> GoldPolicyV3:
        has_humans = bool(self.human_author_ids)
        if (self.authorship == GoldAuthorshipV3.HUMAN_PRE_SUT_SPEC) != has_humans:
            raise ValueError("human gold authorship requires explicit human author IDs")
        return self


class JudgmentPolicyV3(FrozenContractV3):
    mode: JudgmentModeV3
    model_binding: GenerationBindingV3 | None = None
    human_judge_ids: tuple[IdentifierV3, ...] = ()

    @model_validator(mode="after")
    def validate_judgment(self) -> JudgmentPolicyV3:
        is_model_judge = self.mode == JudgmentModeV3.MODEL_JUDGE
        if is_model_judge != (self.model_binding is not None):
            raise ValueError("model_judge requires exactly one frozen model binding")
        is_human_review = self.mode == JudgmentModeV3.HUMAN_REVIEW
        if is_human_review != bool(self.human_judge_ids):
            raise ValueError("human review requires explicit human judge IDs")
        if not is_human_review and self.human_judge_ids:
            raise ValueError("non-human judgment cannot declare human judge IDs")
        return self


class EvaluationVariantV3(FrozenContractV3):
    variant_id: VariantIdV3
    description: str = Field(min_length=1, max_length=1_000)
    agent_topology: AgentTopologyV3
    planning_mode: PlanningModeV3
    max_continuations: Literal[0, 1]
    rag_enabled: bool
    generation_binding: GenerationBindingV3

    @model_validator(mode="after")
    def validate_frozen_behavior(self) -> EvaluationVariantV3:
        expected = {
            "sa_shared_tools_rag": (
                AgentTopologyV3.SINGLE,
                PlanningModeV3.SHARED,
                0,
                True,
            ),
            "ma_fixed_rag": (
                AgentTopologyV3.MULTI,
                PlanningModeV3.FIXED,
                0,
                True,
            ),
            "ma_adaptive_rag": (
                AgentTopologyV3.MULTI,
                PlanningModeV3.ADAPTIVE,
                1,
                True,
            ),
            "ma_adaptive_no_rag": (
                AgentTopologyV3.MULTI,
                PlanningModeV3.ADAPTIVE,
                1,
                False,
            ),
        }[self.variant_id]
        actual = (
            self.agent_topology,
            self.planning_mode,
            self.max_continuations,
            self.rag_enabled,
        )
        if actual != expected:
            raise ValueError(f"variant {self.variant_id!r} does not match Package 7")
        return self


class PilotCaseBindingV3(FrozenContractV3):
    case_id: PilotCaseIdV3
    work_group_id: IdentifierV3
    split: Literal["development"] = "development"


class BudgetPolicyV3(FrozenContractV3):
    corpus_allocation_usd: Decimal = Field(default=Decimal("5"), ge=0)
    pilot_allocation_usd: Decimal = Field(default=Decimal("10"), ge=0)
    benchmark_allocation_usd: Decimal = Field(default=Decimal("70"), ge=0)
    reserve_allocation_usd: Decimal = Field(default=Decimal("15"), ge=0)
    account_warning_usd: Decimal = Field(default=Decimal("50"), ge=0)
    reservation_cap_usd: Decimal = Field(default=Decimal("100"), ge=0)
    per_turn_limit_usd: Decimal = Field(default=Decimal("0.25"), gt=0)
    max_generation_calls_per_turn: int = Field(default=10, ge=1)
    max_provider_attempts_per_turn: int = Field(default=16, ge=1)
    provider_concurrency: int = Field(default=2, ge=1)
    max_retries: int = Field(default=1, ge=0)
    attempt_timeout_seconds: float = Field(default=18.0, gt=0)
    turn_deadline_seconds: float = Field(default=60.0, gt=0)
    max_input_tokens_per_generation: int = Field(default=12_000, ge=1)
    max_output_tokens_per_generation: int = Field(default=1_200, ge=1)

    @model_validator(mode="after")
    def validate_frozen_budget(self) -> BudgetPolicyV3:
        monetary = (
            self.corpus_allocation_usd,
            self.pilot_allocation_usd,
            self.benchmark_allocation_usd,
            self.reserve_allocation_usd,
            self.account_warning_usd,
            self.reservation_cap_usd,
            self.per_turn_limit_usd,
        )
        expected_monetary = tuple(
            Decimal(value) for value in ("5", "10", "70", "15", "50", "100", "0.25")
        )
        if monetary != expected_monetary:
            raise ValueError("Package 7 monetary allocations and limits are frozen")
        controls = (
            self.max_generation_calls_per_turn,
            self.max_provider_attempts_per_turn,
            self.provider_concurrency,
            self.max_retries,
            self.attempt_timeout_seconds,
            self.turn_deadline_seconds,
            self.max_input_tokens_per_generation,
            self.max_output_tokens_per_generation,
        )
        if controls != (10, 16, 2, 1, 18.0, 60.0, 12_000, 1_200):
            raise ValueError("Package 7 provider resource limits are frozen")
        if sum(monetary[:4], Decimal("0")) != self.reservation_cap_usd:
            raise ValueError("evaluation allocations must equal the reservation cap")
        if self.account_warning_usd > self.reservation_cap_usd:
            raise ValueError("account warning cannot exceed reservation cap")
        if self.max_provider_attempts_per_turn < self.max_generation_calls_per_turn:
            raise ValueError("provider attempt cap cannot be below generation call cap")
        if self.attempt_timeout_seconds > self.turn_deadline_seconds:
            raise ValueError("attempt timeout cannot exceed the turn deadline")
        return self


class GlobalRepeatPolicyV3(FrozenContractV3):
    decision_scope: Literal["global"] = "global"
    heldout_conversation_count: Literal[60] = 60
    candidate_repeats: tuple[Literal[3, 2], Literal[3, 2]] = (3, 2)
    benchmark_limit_usd: Decimal = Field(default=Decimal("70"), ge=0)
    effective_cost_rule: Literal["known_plus_unresolved_reserved_maximum"] = (
        "known_plus_unresolved_reserved_maximum"
    )
    warmup_counts_toward_pilot: Literal[True] = True
    warmup_excluded_from_projection: Literal[True] = True
    missing_or_invalid_ledger_policy: Literal["block"] = "block"

    @model_validator(mode="after")
    def validate_repeat_policy(self) -> GlobalRepeatPolicyV3:
        if self.candidate_repeats != (3, 2):
            raise ValueError("global repeat candidates must be tried in order 3 then 2")
        if self.benchmark_limit_usd != Decimal("70"):
            raise ValueError("Package 7 benchmark allocation is frozen at 70 USD")
        return self


class EvaluationExperimentConfigV3(FrozenContractV3):
    schema_version: Literal["3.0"] = "3.0"
    experiment_id: IdentifierV3
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)
    pilot_repeats: Literal[1] = 1
    warmup_repeats: Literal[1] = 1
    warmup_case_id: PilotCaseIdV3
    variants: tuple[EvaluationVariantV3, ...] = Field(min_length=4, max_length=4)
    pilot_cases: tuple[PilotCaseBindingV3, ...] = Field(min_length=8, max_length=8)
    metrics: tuple[EvaluationMetricV3, ...]
    gold_policy: GoldPolicyV3
    judgment_policy: JudgmentPolicyV3
    budget: BudgetPolicyV3
    repeat_policy: GlobalRepeatPolicyV3

    @model_validator(mode="after")
    def validate_package7_experiment(self) -> EvaluationExperimentConfigV3:
        variant_order = tuple(variant.variant_id for variant in self.variants)
        if variant_order != PACKAGE7_VARIANT_ORDER:
            raise ValueError("Package 7 variants must use the frozen order")
        case_order = tuple(case.case_id for case in self.pilot_cases)
        if case_order != PACKAGE7_PILOT_CASE_ORDER:
            raise ValueError("Package 7 pilot cases must use the frozen order")
        if self.warmup_case_id not in case_order:
            raise ValueError("warmup case must be one of the pilot cases")
        bindings = {variant.generation_binding for variant in self.variants}
        if len(bindings) != 1:
            raise ValueError("all Package 7 variants must share one model snapshot")
        if (
            self.judgment_policy.model_binding is not None
            and self.judgment_policy.model_binding
            != self.variants[0].generation_binding
        ):
            raise ValueError("model judge must use the frozen generation snapshot")
        adaptive = self.variants[2].model_dump(
            exclude={"variant_id", "description", "rag_enabled"}
        )
        no_rag = self.variants[3].model_dump(
            exclude={"variant_id", "description", "rag_enabled"}
        )
        if adaptive != no_rag:
            raise ValueError("adaptive no-RAG may differ only by the RAG switch")
        if self.metrics != PACKAGE7_METRICS:
            raise ValueError("Package 7 metrics must use the frozen complete set")
        return self


class EvaluationAssetBindingsV3(FrozenContractV3):
    evaluator_sha256: Sha256V3
    gold_sha256: Sha256V3
    split_sha256: Sha256V3
    tool_contract_sha256: Sha256V3
    prompt_bundle_sha256: Sha256V3
    corpus_sha256: Sha256V3
    index_sha256: Sha256V3
    embedding_sha256: Sha256V3
    pricing_sha256: Sha256V3
    ledger_contract_sha256: Sha256V3
    scoring_rubric_sha256: Sha256V3
    evaluator_configuration_sha256: Sha256V3
    judge_prompt_sha256: Sha256V3
    judge_schema_sha256: Sha256V3
    embedding_model_dimensions_sha256: Sha256V3
    pricing_manifest_sha256: Sha256V3
    usage_ledger_sha256: Sha256V3
    rubric_sha256: Sha256V3

    @model_validator(mode="before")
    @classmethod
    def populate_explicit_provenance_aliases(cls, value: object) -> object:
        """Keep old constructors valid while serializing every v3 binding explicitly."""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        aliases = {
            "evaluator_configuration_sha256": "evaluator_sha256",
            "judge_prompt_sha256": "prompt_bundle_sha256",
            "judge_schema_sha256": "evaluator_sha256",
            "embedding_model_dimensions_sha256": "embedding_sha256",
            "pricing_manifest_sha256": "pricing_sha256",
            "usage_ledger_sha256": "ledger_contract_sha256",
            "rubric_sha256": "scoring_rubric_sha256",
        }
        for explicit_name, legacy_name in aliases.items():
            if explicit_name not in payload and legacy_name in payload:
                payload[explicit_name] = payload[legacy_name]
        return payload


class EvaluationProtocolV3(FrozenContractV3):
    schema_version: Literal["3.0"] = "3.0"
    protocol_id: IdentifierV3
    experiment_sha256: Sha256V3
    experiment: EvaluationExperimentConfigV3
    assets: EvaluationAssetBindingsV3
    case_bindings_sha256: Sha256V3
    variant_set_sha256: Sha256V3
    budget_sha256: Sha256V3
    schedule_algorithm_id: Literal["package7_pilot_interleaved_v1"]
    schedule_algorithm_sha256: Sha256V3
    repeat_rule_id: Literal["package7_global_cost_gate_v1"]
    repeat_rule_sha256: Sha256V3

    @model_validator(mode="after")
    def validate_protocol_identity(self) -> EvaluationProtocolV3:
        if self.protocol_id != self.experiment.experiment_id:
            raise ValueError("protocol ID must equal the frozen experiment ID")
        return self

    @property
    def variants(self) -> tuple[EvaluationVariantV3, ...]:
        return self.experiment.variants

    @property
    def pilot_cases(self) -> tuple[PilotCaseBindingV3, ...]:
        return self.experiment.pilot_cases

    @property
    def random_seed(self) -> int:
        return self.experiment.random_seed


class ObservationIdentityV3(FrozenContractV3):
    schema_version: Literal["3.0"] = "3.0"
    run_id: IdentifierV3
    protocol_sha256: Sha256V3
    schedule_algorithm_id: Literal["package7_pilot_interleaved_v1"]
    turn_kind: ScheduledTurnKindV3
    variant_id: VariantIdV3
    case_id: CaseIdV3
    work_group_id: IdentifierV3
    repetition: int = Field(ge=0)


def canonical_turn_id_v3(identity: ObservationIdentityV3) -> str:
    return f"turn_{canonical_sha256(identity)}"


def canonical_observation_id_v3(identity: ObservationIdentityV3) -> str:
    return f"obs_{canonical_sha256(identity)}"


class ScheduledTurnV3(FrozenContractV3):
    turn_id: IdentifierV3
    observation_id: IdentifierV3 | None = None
    identity: ObservationIdentityV3
    schedule_index: int = Field(ge=0)
    execution_order: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_kind(self) -> ScheduledTurnV3:
        if self.turn_id != canonical_turn_id_v3(self.identity):
            raise ValueError("turn ID does not match its canonical identity")
        is_measured = self.identity.turn_kind == ScheduledTurnKindV3.MEASURED
        if is_measured != (self.observation_id is not None):
            raise ValueError("only measured turns have observation IDs")
        if is_measured != (self.execution_order is not None):
            raise ValueError("only measured turns have execution order")
        if is_measured and self.observation_id != canonical_observation_id_v3(
            self.identity
        ):
            raise ValueError("observation ID does not match its canonical identity")
        return self


class EvaluationObservationV3(FrozenContractV3):
    schema_version: Literal["3.0"] = "3.0"
    observation_id: IdentifierV3
    identity: ObservationIdentityV3
    run_id: IdentifierV3
    protocol_sha256: Sha256V3
    variant_id: VariantIdV3
    case_id: CaseIdV3
    work_group_id: IdentifierV3
    repetition: int = Field(ge=0)
    execution_order: int = Field(ge=0)
    status: TaskStatus
    task_completed: bool
    answerable: bool
    abstained: bool
    claim_support: float | None = Field(default=None, ge=0, le=1)
    citation_precision: float | None = Field(default=None, ge=0, le=1)
    citation_coverage: float | None = Field(default=None, ge=0, le=1)
    document_recall: float | None = Field(default=None, ge=0, le=1)
    authorized: bool
    valid_plan: bool
    useful_continuation: bool | None = None
    duplicate_dispatch_count: int = Field(ge=0)
    end_to_end_latency_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    known_cost_usd: Decimal = Field(ge=0)
    unresolved_reserved_cost_usd: Decimal = Field(ge=0)
    ledger_event_ids: tuple[IdentifierV3, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_usage(self) -> EvaluationObservationV3:
        if self.identity.turn_kind != ScheduledTurnKindV3.MEASURED:
            raise ValueError("evaluation observations require a measured identity")
        if self.observation_id != canonical_observation_id_v3(self.identity):
            raise ValueError("observation ID does not match its canonical identity")
        mirrored_identity = (
            self.run_id,
            self.protocol_sha256,
            self.variant_id,
            self.case_id,
            self.work_group_id,
            self.repetition,
        )
        identity_values = (
            self.identity.run_id,
            self.identity.protocol_sha256,
            self.identity.variant_id,
            self.identity.case_id,
            self.identity.work_group_id,
            self.identity.repetition,
        )
        if mirrored_identity != identity_values:
            raise ValueError("observation fields do not match its canonical identity")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total tokens must equal input plus output tokens")
        if len(self.ledger_event_ids) != len(set(self.ledger_event_ids)):
            raise ValueError("ledger event IDs must be unique")
        return self

    @property
    def effective_cost_usd(self) -> Decimal:
        return self.known_cost_usd + self.unresolved_reserved_cost_usd


class PilotTurnCostV3(FrozenContractV3):
    turn_id: IdentifierV3
    identity: ObservationIdentityV3
    variant_id: VariantIdV3
    case_id: CaseIdV3
    is_warmup: bool
    known_cost_usd: Decimal = Field(ge=0)
    unresolved_reservation_maxima_usd: tuple[Decimal, ...] = ()
    ledger_attributed: bool
    ledger_valid: bool
    ledger_event_ids: tuple[IdentifierV3, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reservations(self) -> PilotTurnCostV3:
        if self.turn_id != canonical_turn_id_v3(self.identity):
            raise ValueError("turn ID does not match its canonical identity")
        if (self.variant_id, self.case_id) != (
            self.identity.variant_id,
            self.identity.case_id,
        ):
            raise ValueError("pilot cost fields do not match its canonical identity")
        identity_is_warmup = self.identity.turn_kind == ScheduledTurnKindV3.WARMUP
        if self.is_warmup != identity_is_warmup:
            raise ValueError("pilot cost warmup label does not match its identity")
        if len(self.ledger_event_ids) != len(set(self.ledger_event_ids)):
            raise ValueError("ledger event IDs must be unique")
        if any(
            value < 0 or not value.is_finite()
            for value in self.unresolved_reservation_maxima_usd
        ):
            raise ValueError(
                "unresolved reservation maxima must be finite and non-negative"
            )
        return self

    @property
    def effective_cost_usd(self) -> Decimal:
        return self.known_cost_usd + sum(
            self.unresolved_reservation_maxima_usd,
            Decimal("0"),
        )


class VariantCostProjectionV3(FrozenContractV3):
    variant_id: VariantIdV3
    maximum_pilot_observation_cost_usd: Decimal = Field(ge=0)


class RepeatDecisionV3(FrozenContractV3):
    schema_version: Literal["3.0"] = "3.0"
    protocol_sha256: Sha256V3
    repeat_rule_sha256: Sha256V3
    cost_evidence_sha256: Sha256V3
    selected_repeats: Literal[2, 3]
    pilot_effective_cost_usd: Decimal = Field(ge=0)
    projected_benchmark_cost_usd: Decimal = Field(ge=0)
    per_variant: tuple[VariantCostProjectionV3, ...] = Field(
        min_length=4,
        max_length=4,
    )

    @model_validator(mode="after")
    def validate_variant_order(self) -> RepeatDecisionV3:
        if (
            tuple(item.variant_id for item in self.per_variant)
            != PACKAGE7_VARIANT_ORDER
        ):
            raise ValueError("repeat cost projection must cover all variants in order")
        return self
