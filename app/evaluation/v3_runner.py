"""Exactly-once Package 7 observation runner over an injected executor boundary.

This module owns scheduling, namespaces, limits, evidence attribution, and
checkpoint transitions.  It deliberately does not know how gold is stored or
how a concrete variant is composed.  Callers adapt read-only cases into
``EvaluationCaseV3`` and inject a fresh executor for each scheduled item.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_checkpoint import (
    CheckpointEventV3,
    CheckpointIntegrityErrorV3,
    CheckpointStateV3,
    CheckpointWriterV3,
)
from app.evaluation.v3_models import (
    BudgetPolicyV3,
    EvaluationProtocolV3,
    ObservationIdentityV3,
    PilotTurnCostV3,
    ScheduledTurnKindV3,
    ScheduledTurnV3,
)
from app.evaluation.v3_protocol import (
    PilotObservationCostEvidenceV3,
    PilotUserTurnCostEvidenceV3,
    evaluation_protocol_sha256_v3,
    validate_evaluation_protocol_v3,
)
from app.evaluation.v3_schedule import pilot_schedule_sha256_v3

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")


class FrozenRunnerContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ObservationResourceLimitErrorV3(RuntimeError):
    """Observed executor evidence exceeds a frozen Package 7 resource limit."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ObservationExecutionFailureV3(RuntimeError):
    """Safe executor failure that may carry already-finalized evidence."""

    def __init__(
        self,
        safe_error_code: str,
        *,
        partial_results: Sequence[UserTurnExecutionResultV3] = (),
    ) -> None:
        if _SAFE_IDENTIFIER.fullmatch(safe_error_code) is None:
            raise ValueError("safe_error_code is not a valid identifier")
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code
        self.partial_results = tuple(partial_results)


class EvaluationResourceLimitsV3(FrozenRunnerContractV3):
    """The frozen controls passed to and checked at the executor boundary."""

    provider_concurrency: int = Field(ge=1)
    max_generation_calls: int = Field(ge=1)
    max_provider_attempts: int = Field(ge=1)
    max_retries: int = Field(ge=0)
    attempt_timeout_seconds: float = Field(gt=0)
    turn_deadline_seconds: float = Field(gt=0)
    per_turn_limit_usd: Decimal = Field(gt=0)
    max_input_tokens_per_generation: int = Field(ge=1)
    max_output_tokens_per_generation: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_package7_limits(self) -> EvaluationResourceLimitsV3:
        actual = (
            self.provider_concurrency,
            self.max_generation_calls,
            self.max_provider_attempts,
            self.max_retries,
            self.attempt_timeout_seconds,
            self.turn_deadline_seconds,
            self.per_turn_limit_usd,
            self.max_input_tokens_per_generation,
            self.max_output_tokens_per_generation,
        )
        expected = (2, 10, 16, 1, 18.0, 60.0, Decimal("0.25"), 12_000, 1_200)
        if actual != expected:
            raise ValueError("executor limits differ from the frozen Package 7 policy")
        return self

    @classmethod
    def from_budget(cls, budget: BudgetPolicyV3) -> Self:
        return cls(
            provider_concurrency=budget.provider_concurrency,
            max_generation_calls=budget.max_generation_calls_per_turn,
            max_provider_attempts=budget.max_provider_attempts_per_turn,
            max_retries=budget.max_retries,
            attempt_timeout_seconds=budget.attempt_timeout_seconds,
            turn_deadline_seconds=budget.turn_deadline_seconds,
            per_turn_limit_usd=budget.per_turn_limit_usd,
            max_input_tokens_per_generation=(budget.max_input_tokens_per_generation),
            max_output_tokens_per_generation=(budget.max_output_tokens_per_generation),
        )


class EvaluationUserTurnV3(FrozenRunnerContractV3):
    source_turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    ordinal: int = Field(ge=1, le=32)
    message: str = Field(min_length=1, max_length=8_000)

    @model_validator(mode="after")
    def validate_message(self) -> EvaluationUserTurnV3:
        if not self.message.strip():
            raise ValueError("evaluation user message cannot be blank")
        return self


class SandboxFixtureAdapterV3(FrozenRunnerContractV3):
    """Content-addressed full sandbox fixture supplied by the gold adapter."""

    fixture_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    fixture_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reset_revision: int = Field(ge=1)
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_fixture(self) -> SandboxFixtureAdapterV3:
        if self.payload.get("fixture_id") != self.fixture_id:
            raise ValueError("sandbox fixture payload and ID disagree")
        if self.payload.get("reset_revision") != self.reset_revision:
            raise ValueError("sandbox fixture payload and reset revision disagree")
        if canonical_sha256(self.payload) != self.fixture_sha256:
            raise ValueError("sandbox fixture SHA-256 does not match its payload")
        return self


class EvaluationCaseV3(FrozenRunnerContractV3):
    """Narrow, read-only adapter consumed by the runner instead of gold models."""

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    work_group_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    category: Literal[
        "multi_constraint",
        "knowledge_source",
        "multi_expert_compare_recommendation",
        "multi_turn_memory",
        "insufficient_conflict_injection",
        "shopping_merchant",
    ]
    principal_role: Literal["shopper", "merchant"]
    scopes: tuple[str, ...] = Field(min_length=1)
    resolved_product_ids: tuple[int, ...] = Field(default=(), max_length=5)
    user_turns: tuple[EvaluationUserTurnV3, ...] = Field(min_length=1, max_length=32)
    initial_state: dict[str, JsonValue]
    sandbox_fixture: SandboxFixtureAdapterV3 | None = None

    @model_validator(mode="after")
    def validate_case(self) -> EvaluationCaseV3:
        if tuple(turn.ordinal for turn in self.user_turns) != tuple(
            range(1, len(self.user_turns) + 1)
        ):
            raise ValueError("case user-turn ordinals must be contiguous from one")
        turn_ids = tuple(turn.source_turn_id for turn in self.user_turns)
        if len(turn_ids) != len(set(turn_ids)):
            raise ValueError("case user-turn IDs must be unique")
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("case scopes must be unique")
        if any(_SAFE_IDENTIFIER.fullmatch(scope) is None for scope in self.scopes):
            raise ValueError("case scope is not a valid identifier")
        is_shopping = self.category == "shopping_merchant"
        if is_shopping != (self.sandbox_fixture is not None):
            raise ValueError("shopping cases require exactly one full sandbox fixture")
        return self

    @field_validator("resolved_product_ids")
    @classmethod
    def validate_resolved_product_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(product_id <= 0 for product_id in value):
            raise ValueError("case product IDs must be positive")
        if len(value) != len(set(value)):
            raise ValueError("case product IDs must be unique")
        return value


def execution_case_sha256_v3(case: EvaluationCaseV3) -> str:
    """Hash every behavior-relevant input supplied to one case execution."""

    materialized = EvaluationCaseV3.model_validate(case)
    return canonical_sha256(
        {
            "execution_case": materialized.model_dump(mode="json"),
            "schema_version": "3.0",
        }
    )


def execution_case_set_sha256_v3(
    schedule: Sequence[ScheduledTurnV3],
    cases: Mapping[str, EvaluationCaseV3],
) -> str:
    """Bind the full scheduled case set and each schedule-to-case relationship."""

    frozen_schedule = tuple(schedule)
    required_case_ids = {turn.identity.case_id for turn in frozen_schedule}
    if set(cases) != required_case_ids:
        raise ValueError("execution case set must exactly cover the supplied schedule")
    materialized = {
        case_id: EvaluationCaseV3.model_validate(cases[case_id])
        for case_id in sorted(required_case_ids)
    }
    case_hashes = {
        case_id: execution_case_sha256_v3(case)
        for case_id, case in materialized.items()
    }
    return canonical_sha256(
        {
            "execution_cases": [
                {
                    "case": materialized[case_id].model_dump(mode="json"),
                    "execution_case_sha256": case_hashes[case_id],
                }
                for case_id in sorted(materialized)
            ],
            "schedule_case_bindings": [
                {
                    "case_id": turn.identity.case_id,
                    "execution_case_sha256": case_hashes[turn.identity.case_id],
                    "schedule_index": turn.schedule_index,
                    "turn_id": turn.turn_id,
                }
                for turn in frozen_schedule
            ],
            "schedule_sha256": pilot_schedule_sha256_v3(frozen_schedule),
            "schema_version": "3.0",
        }
    )


class ExecutionNamespaceV3(FrozenRunnerContractV3):
    namespace_id: str = Field(pattern=r"^namespace_[a-f0-9]{64}$")
    tenant_id: str = Field(pattern=r"^tenant_[a-f0-9]{64}$")
    principal_id: str = Field(pattern=r"^principal_[a-f0-9]{64}$")
    conversation_id: str = Field(pattern=r"^conversation_[a-f0-9]{64}$")


class ObservationExecutionContextV3(FrozenRunnerContractV3):
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_index: int = Field(ge=0)
    canonical_turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    observation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    identity: ObservationIdentityV3
    namespace: ExecutionNamespaceV3
    limits: EvaluationResourceLimitsV3

    @model_validator(mode="after")
    def validate_identity(self) -> ObservationExecutionContextV3:
        mirrored = (
            self.run_id,
            self.protocol_sha256,
            self.canonical_turn_id,
            self.observation_id,
        )
        expected = (
            self.identity.run_id,
            self.identity.protocol_sha256,
            _canonical_turn_id(self.identity),
            (
                _canonical_observation_id(self.identity)
                if self.identity.turn_kind is ScheduledTurnKindV3.MEASURED
                else None
            ),
        )
        if mirrored != expected:
            raise ValueError("execution context differs from its canonical identity")
        if self.namespace != execution_namespace_v3(
            run_id=self.run_id,
            protocol_sha256=self.protocol_sha256,
            execution_case_sha256=self.execution_case_sha256,
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            canonical_turn_id=self.canonical_turn_id,
            observation_id=self.observation_id,
        ):
            raise ValueError("execution namespace is not canonical")
        return self


class InitialStateResetReceiptV3(FrozenRunnerContractV3):
    """Explicit executor acknowledgment required before the first user turn."""

    reset_id: str = Field(pattern=r"^reset_[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    execution_case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    observation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    namespace_id: str = Field(pattern=r"^namespace_[a-f0-9]{64}$")
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    work_group_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    initial_state_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sandbox_fixture_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    sandbox_fixture_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    reset_revision: int | None = Field(default=None, ge=1)
    reset_completed: Literal[True] = True

    @model_validator(mode="after")
    def validate_fixture_binding(self) -> InitialStateResetReceiptV3:
        bindings = (
            self.sandbox_fixture_id,
            self.sandbox_fixture_sha256,
            self.reset_revision,
        )
        if any(value is None for value in bindings) and any(
            value is not None for value in bindings
        ):
            raise ValueError("reset receipt has a partial sandbox fixture binding")
        return self


class ExecutionAttributionV3(FrozenRunnerContractV3):
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    canonical_turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    observation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    execution_turn_id: str = Field(pattern=r"^eturn_[a-f0-9]{64}$")
    source_turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")


class UserTurnExecutionRequestV3(FrozenRunnerContractV3):
    context: ObservationExecutionContextV3
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    work_group_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    principal_role: Literal["shopper", "merchant"]
    scopes: tuple[str, ...] = Field(min_length=1)
    user_turn: EvaluationUserTurnV3
    attribution: ExecutionAttributionV3
    client_turn_id: str = Field(pattern=r"^client_[a-f0-9]{64}$")
    request_id: str = Field(pattern=r"^request_[a-f0-9]{64}$")
    trace_id: str = Field(pattern=r"^trace_[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_request_ids(self) -> UserTurnExecutionRequestV3:
        expected_attribution = execution_attribution_v3(
            self.context,
            self.user_turn,
        )
        if self.attribution != expected_attribution:
            raise ValueError("turn request attribution is not canonical")
        expected = _request_ids(self.context, self.user_turn)
        if (self.client_turn_id, self.request_id, self.trace_id) != expected:
            raise ValueError("turn request correlation IDs are not canonical")
        return self


class ModelCallEvidenceV3(FrozenRunnerContractV3):
    call_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    attribution: ExecutionAttributionV3
    model: str = Field(min_length=1, max_length=128)
    attempts: int = Field(ge=0, le=16)
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_usage(self) -> ModelCallEvidenceV3:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens exceed model input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens must be included in output tokens")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("model total tokens do not reconcile")
        return self


class EmbeddingCallEvidenceV3(FrozenRunnerContractV3):
    call_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    attribution: ExecutionAttributionV3
    model: str = Field(min_length=1, max_length=128)
    attempts: int = Field(ge=0, le=16)
    input_tokens: int = Field(ge=0)


class RetryEvidenceV3(FrozenRunnerContractV3):
    retry_event_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    attribution: ExecutionAttributionV3
    call_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    retry_ordinal: int = Field(ge=1, le=16)


class LedgerEventKindV3(StrEnum):
    SETTLED_KNOWN = "settled_known"
    UNRESOLVED_RESERVATION = "unresolved_reservation"
    RELEASED = "released"
    ZERO_COST = "zero_cost"


class LedgerEventEvidenceV3(FrozenRunnerContractV3):
    ledger_event_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    attribution: ExecutionAttributionV3
    kind: LedgerEventKindV3
    reservation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    known_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    unresolved_reserved_maximum_usd: Decimal = Field(
        default=Decimal("0"),
        ge=0,
    )

    @model_validator(mode="after")
    def validate_ledger_kind(self) -> LedgerEventEvidenceV3:
        unresolved = self.unresolved_reserved_maximum_usd
        if self.kind is LedgerEventKindV3.UNRESOLVED_RESERVATION:
            if self.reservation_id is None or unresolved <= 0:
                raise ValueError(
                    "unresolved reservation evidence requires an ID and maximum"
                )
        elif unresolved != 0:
            raise ValueError(
                "only unresolved reservation evidence may retain reserved cost"
            )
        if self.kind in {LedgerEventKindV3.RELEASED, LedgerEventKindV3.ZERO_COST}:
            if self.known_cost_usd != 0:
                raise ValueError("released or zero-cost evidence cannot claim cost")
        return self


class UserTurnExecutionResultV3(FrozenRunnerContractV3):
    attribution: ExecutionAttributionV3
    result_payload: dict[str, JsonValue]
    model_calls: tuple[ModelCallEvidenceV3, ...] = ()
    embedding_calls: tuple[EmbeddingCallEvidenceV3, ...] = ()
    retry_events: tuple[RetryEvidenceV3, ...] = ()
    ledger_events: tuple[LedgerEventEvidenceV3, ...] = Field(min_length=1)
    peak_provider_concurrency: int = Field(ge=0)
    max_attempt_duration_seconds: float = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_evidence(self) -> UserTurnExecutionResultV3:
        attributions = (
            *(item.attribution for item in self.model_calls),
            *(item.attribution for item in self.embedding_calls),
            *(item.attribution for item in self.retry_events),
            *(item.attribution for item in self.ledger_events),
        )
        if any(item != self.attribution for item in attributions):
            raise ValueError("executor evidence is not bound to its canonical turn")
        call_ids = tuple(call.call_id for call in self.model_calls) + tuple(
            call.call_id for call in self.embedding_calls
        )
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("provider call evidence IDs must be unique")
        ledger_ids = tuple(item.ledger_event_id for item in self.ledger_events)
        if len(ledger_ids) != len(set(ledger_ids)):
            raise ValueError("ledger event IDs must be unique")
        retry_ids = tuple(item.retry_event_id for item in self.retry_events)
        if len(retry_ids) != len(set(retry_ids)):
            raise ValueError("retry event IDs must be unique")
        observed_retries = {
            (item.call_id, item.retry_ordinal) for item in self.retry_events
        }
        expected_retries = {
            (call.call_id, retry_ordinal)
            for call in self.model_calls
            for retry_ordinal in range(1, call.attempts)
        } | {
            (call.call_id, retry_ordinal)
            for call in self.embedding_calls
            for retry_ordinal in range(1, call.attempts)
        }
        if observed_retries != expected_retries:
            raise ValueError("retry evidence does not match provider attempts")
        attempts = sum(call.attempts for call in self.model_calls) + sum(
            call.attempts for call in self.embedding_calls
        )
        if attempts == 0 and self.peak_provider_concurrency != 0:
            raise ValueError("zero-attempt evidence cannot report provider concurrency")
        if attempts > 0 and self.peak_provider_concurrency < 1:
            raise ValueError("provider attempts require observed concurrency")
        return self

    @property
    def generation_calls(self) -> int:
        return len(self.model_calls)

    @property
    def provider_attempts(self) -> int:
        return sum(call.attempts for call in self.model_calls) + sum(
            call.attempts for call in self.embedding_calls
        )

    @property
    def input_tokens(self) -> int:
        return sum(call.input_tokens for call in self.model_calls) + sum(
            call.input_tokens for call in self.embedding_calls
        )

    @property
    def output_tokens(self) -> int:
        return sum(call.output_tokens for call in self.model_calls)

    @property
    def known_cost_usd(self) -> Decimal:
        return sum(
            (event.known_cost_usd for event in self.ledger_events),
            Decimal("0"),
        )

    @property
    def unresolved_reserved_cost_usd(self) -> Decimal:
        return sum(
            (event.unresolved_reserved_maximum_usd for event in self.ledger_events),
            Decimal("0"),
        )


class ObservationResourceTotalsV3(FrozenRunnerContractV3):
    generation_calls: int = Field(ge=0)
    provider_attempts: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    peak_provider_concurrency: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    known_cost_usd: Decimal = Field(ge=0)
    unresolved_reserved_cost_usd: Decimal = Field(ge=0)

    @property
    def effective_cost_usd(self) -> Decimal:
        return self.known_cost_usd + self.unresolved_reserved_cost_usd


class ObservationTerminalStatusV3(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class AmbiguousObservationReceiptV3(FrozenRunnerContractV3):
    """Durable non-execution marker for one orphaned started observation."""

    schema_version: Literal["3.0"] = "3.0"
    safe_reason: Literal["orphan_started_no_replay"] = "orphan_started_no_replay"
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_index: int = Field(ge=0)
    canonical_turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    observation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )


class ObservationRunReceiptV3(FrozenRunnerContractV3):
    schema_version: Literal["3.0"] = "3.0"
    terminal_status: ObservationTerminalStatusV3
    safe_error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_index: int = Field(ge=0)
    execution_order: int | None = Field(default=None, ge=0)
    canonical_turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    observation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    identity: ObservationIdentityV3
    namespace: ExecutionNamespaceV3
    limits: EvaluationResourceLimitsV3
    initial_state_reset: bool
    initial_state_reset_receipt: InitialStateResetReceiptV3 | None
    turn_results: tuple[UserTurnExecutionResultV3, ...]
    totals: ObservationResourceTotalsV3
    pilot_cost: PilotTurnCostV3 | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> ObservationRunReceiptV3:
        if self.terminal_status is ObservationTerminalStatusV3.COMPLETED:
            if self.safe_error_code is not None:
                raise ValueError("completed receipt cannot contain an error")
            if (
                not self.initial_state_reset
                or self.initial_state_reset_receipt is None
                or not self.turn_results
            ):
                raise ValueError(
                    "completed receipt requires reset and user-turn evidence"
                )
            if self.pilot_cost is None:
                raise ValueError("completed receipt requires pilot cost evidence")
        elif self.safe_error_code is None:
            raise ValueError("failed receipt requires a safe error code")
        if self.initial_state_reset != (self.initial_state_reset_receipt is not None):
            raise ValueError("receipt reset flag and acknowledgment disagree")
        expected_observation = (
            _canonical_observation_id(self.identity)
            if self.identity.turn_kind is ScheduledTurnKindV3.MEASURED
            else None
        )
        mirrored = (
            self.run_id,
            self.protocol_sha256,
            self.canonical_turn_id,
            self.observation_id,
        )
        expected = (
            self.identity.run_id,
            self.identity.protocol_sha256,
            _canonical_turn_id(self.identity),
            expected_observation,
        )
        if mirrored != expected:
            raise ValueError("receipt differs from its canonical observation identity")
        expected_namespace = execution_namespace_v3(
            run_id=self.run_id,
            protocol_sha256=self.protocol_sha256,
            execution_case_sha256=self.execution_case_sha256,
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            canonical_turn_id=self.canonical_turn_id,
            observation_id=self.observation_id,
        )
        if self.namespace != expected_namespace:
            raise ValueError("receipt execution namespace is not canonical")
        reset_receipt = self.initial_state_reset_receipt
        if reset_receipt is not None and (
            reset_receipt.run_id != self.run_id
            or reset_receipt.execution_case_sha256 != self.execution_case_sha256
            or reset_receipt.execution_case_set_sha256 != self.execution_case_set_sha256
            or reset_receipt.canonical_turn_id != self.canonical_turn_id
            or reset_receipt.observation_id != self.observation_id
            or reset_receipt.namespace_id != self.namespace.namespace_id
        ):
            raise ValueError("receipt reset acknowledgment has foreign attribution")
        if any(
            (
                result.attribution.run_id != self.run_id
                or result.attribution.canonical_turn_id != self.canonical_turn_id
                or result.attribution.observation_id != self.observation_id
            )
            for result in self.turn_results
        ):
            raise ValueError("receipt contains foreign execution attribution")
        if self.totals != _resource_totals(self.turn_results):
            raise ValueError("receipt resource totals do not reconcile")
        ledger_events = tuple(
            event for result in self.turn_results for event in result.ledger_events
        )
        ledger_ids = tuple(event.ledger_event_id for event in ledger_events)
        if len(ledger_ids) != len(set(ledger_ids)):
            raise ValueError("receipt reuses ledger event IDs")
        if bool(ledger_events) != (self.pilot_cost is not None):
            raise ValueError("receipt pilot cost and ledger evidence disagree")
        if self.pilot_cost is not None:
            reservation_maxima = tuple(
                event.unresolved_reserved_maximum_usd
                for event in ledger_events
                if event.unresolved_reserved_maximum_usd > 0
            )
            if (
                self.pilot_cost.turn_id != self.canonical_turn_id
                or self.pilot_cost.identity != self.identity
                or self.pilot_cost.known_cost_usd != self.totals.known_cost_usd
                or self.pilot_cost.unresolved_reservation_maxima_usd
                != reservation_maxima
                or self.pilot_cost.ledger_event_ids != ledger_ids
            ):
                raise ValueError("receipt pilot cost does not match its evidence")
        return self


class EvaluationRunSummaryV3(FrozenRunnerContractV3):
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    total_scheduled: int = Field(ge=0)
    completed_turn_ids: tuple[str, ...]
    failed_turn_ids: tuple[str, ...]
    pending_turn_ids: tuple[str, ...]
    missing_turn_ids: tuple[str, ...]
    ambiguous_turn_ids: tuple[str, ...]
    orphan_started_turn_ids: tuple[str, ...]
    completed_observation_ids: tuple[str, ...]
    is_partial: bool
    blocked_on_ambiguous_work: bool


class EvaluationRunResultV3(FrozenRunnerContractV3):
    summary: EvaluationRunSummaryV3
    receipts: tuple[ObservationRunReceiptV3, ...]

    @property
    def pilot_costs(self) -> tuple[PilotTurnCostV3, ...]:
        return tuple(
            receipt.pilot_cost
            for receipt in self.receipts
            if receipt.pilot_cost is not None
        )

    @property
    def pilot_cost_evidence(self) -> tuple[PilotObservationCostEvidenceV3, ...]:
        return tuple(
            _pilot_observation_cost_evidence(receipt)
            for receipt in self.receipts
            if receipt.pilot_cost is not None
        )


class ObservationExecutorV3(Protocol):
    async def reset_initial_state(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> InitialStateResetReceiptV3: ...

    async def execute_turn(
        self,
        request: UserTurnExecutionRequestV3,
    ) -> UserTurnExecutionResultV3: ...


class ObservationExecutorFactoryV3(Protocol):
    def __call__(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> ObservationExecutorV3: ...


class EvaluationV3ObservationRunner:
    """Run or resume one schedule while retaining the checkpoint writer lock."""

    def __init__(
        self,
        *,
        protocol: EvaluationProtocolV3,
        schedule: Sequence[ScheduledTurnV3],
        checkpoint_path: Path,
        cases: Mapping[str, EvaluationCaseV3],
        executor_factory: ObservationExecutorFactoryV3,
    ) -> None:
        validate_evaluation_protocol_v3(protocol)
        self.protocol = protocol
        self.protocol_sha256 = evaluation_protocol_sha256_v3(protocol)
        self.schedule = tuple(schedule)
        self.schedule_sha256 = pilot_schedule_sha256_v3(self.schedule)
        self.run_id = _schedule_run_id(self.schedule)
        self.limits = EvaluationResourceLimitsV3.from_budget(protocol.experiment.budget)
        self.checkpoint_path = checkpoint_path
        self.cases = _validate_cases(protocol, self.schedule, cases)
        self.execution_case_sha256_by_id = {
            case_id: execution_case_sha256_v3(case)
            for case_id, case in self.cases.items()
        }
        self.execution_case_set_sha256 = execution_case_set_sha256_v3(
            self.schedule,
            self.cases,
        )
        self.executor_factory = executor_factory
        _validate_schedule_against_protocol(
            protocol,
            self.schedule,
            run_id=self.run_id,
            protocol_sha256=self.protocol_sha256,
        )

    async def run(self) -> EvaluationRunResultV3:
        with CheckpointWriterV3(
            self.checkpoint_path,
            run_id=self.run_id,
            protocol_sha256=self.protocol_sha256,
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            schedule=self.schedule,
        ) as checkpoint:
            _load_terminal_receipts(
                checkpoint.state,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha256,
                cases=self.cases,
                execution_case_sha256_by_id=self.execution_case_sha256_by_id,
                execution_case_set_sha256=self.execution_case_set_sha256,
                limits=self.limits,
            )
            turns_by_id = {turn.turn_id: turn for turn in self.schedule}
            for orphan_turn_id in checkpoint.state.orphan_started_turn_ids:
                orphan = turns_by_id[orphan_turn_id]
                ambiguity = AmbiguousObservationReceiptV3(
                    run_id=self.run_id,
                    protocol_sha256=self.protocol_sha256,
                    execution_case_sha256=self.execution_case_sha256_by_id[
                        orphan.identity.case_id
                    ],
                    execution_case_set_sha256=self.execution_case_set_sha256,
                    schedule_sha256=self.schedule_sha256,
                    schedule_index=orphan.schedule_index,
                    canonical_turn_id=orphan.turn_id,
                    observation_id=orphan.observation_id,
                )
                checkpoint.append_ambiguous(
                    orphan,
                    {"ambiguity": ambiguity.model_dump(mode="json")},
                )

            terminal_ids = (
                set(checkpoint.state.completed_turn_ids)
                | set(checkpoint.state.failed_turn_ids)
                | set(checkpoint.state.ambiguous_turn_ids)
            )
            for turn in self.schedule:
                if turn.turn_id in terminal_ids:
                    continue
                checkpoint.append_started(turn)
                receipt = await self._execute_observation(turn)
                payload: dict[str, JsonValue] = {
                    "receipt": receipt.model_dump(mode="json")
                }
                if receipt.terminal_status is ObservationTerminalStatusV3.COMPLETED:
                    checkpoint.append_completed(turn, payload)
                else:
                    checkpoint.append_failed(turn, payload)
                terminal_ids.add(turn.turn_id)

            return _run_result(
                checkpoint.state,
                schedule=self.schedule,
                run_id=self.run_id,
                protocol_sha256=self.protocol_sha256,
                execution_case_set_sha256=self.execution_case_set_sha256,
                schedule_sha256=self.schedule_sha256,
                cases=self.cases,
                execution_case_sha256_by_id=self.execution_case_sha256_by_id,
                limits=self.limits,
            )

    async def _execute_observation(
        self,
        turn: ScheduledTurnV3,
    ) -> ObservationRunReceiptV3:
        case = self.cases[turn.identity.case_id]
        context = _execution_context(
            turn,
            execution_case_sha256=self.execution_case_sha256_by_id[case.case_id],
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            limits=self.limits,
        )
        results: list[UserTurnExecutionResultV3] = []
        reset_completed = False
        reset_receipt: InitialStateResetReceiptV3 | None = None
        safe_error_code: str | None = None
        try:
            executor = self.executor_factory(context=context, case=case)
            async with asyncio.timeout(self.limits.turn_deadline_seconds):
                candidate_reset = await executor.reset_initial_state(
                    context=context,
                    case=case,
                )
                if not isinstance(candidate_reset, InitialStateResetReceiptV3):
                    raise ObservationExecutionFailureV3(
                        "initial_state_reset_unacknowledged"
                    )
                expected_reset = initial_state_reset_receipt_v3(context, case)
                if candidate_reset != expected_reset:
                    raise ObservationExecutionFailureV3(
                        "initial_state_reset_binding_invalid"
                    )
                reset_receipt = candidate_reset
                reset_completed = True
            for user_turn in case.user_turns:
                async with asyncio.timeout(self.limits.turn_deadline_seconds):
                    request = _turn_request(context, case, user_turn)
                    result = await executor.execute_turn(request)
                    if not isinstance(result, UserTurnExecutionResultV3):
                        raise ObservationExecutionFailureV3(
                            "executor_receipt_type_invalid"
                        )
                    _validate_turn_result(result, request)
                    candidate_results = (*results, result)
                    _validate_observation_results(
                        candidate_results,
                        context=context,
                        case=case,
                    )
                    results.append(result)
                    _enforce_resource_limits(candidate_results, self.limits)
        except ObservationExecutionFailureV3 as exc:
            safe_error_code = exc.safe_error_code
            try:
                _merge_partial_results(
                    results,
                    exc.partial_results,
                    context=context,
                    case=case,
                )
                _enforce_resource_limits(results, self.limits)
            except ObservationExecutionFailureV3 as partial_error:
                safe_error_code = partial_error.safe_error_code
            except ObservationResourceLimitErrorV3 as partial_error:
                safe_error_code = partial_error.code
        except ObservationResourceLimitErrorV3 as exc:
            safe_error_code = exc.code
        except TimeoutError:
            safe_error_code = "turn_deadline_exceeded"
        except Exception:
            safe_error_code = "observation_executor_failed"

        terminal_status = (
            ObservationTerminalStatusV3.COMPLETED
            if safe_error_code is None
            else ObservationTerminalStatusV3.FAILED
        )
        if terminal_status is ObservationTerminalStatusV3.COMPLETED:
            expected_source_ids = tuple(item.source_turn_id for item in case.user_turns)
            actual_source_ids = tuple(
                item.attribution.source_turn_id for item in results
            )
            if actual_source_ids != expected_source_ids:
                terminal_status = ObservationTerminalStatusV3.FAILED
                safe_error_code = "executor_turn_sequence_invalid"

        return _build_receipt(
            turn,
            context=context,
            terminal_status=terminal_status,
            safe_error_code=safe_error_code,
            initial_state_reset=reset_completed,
            initial_state_reset_receipt=reset_receipt,
            results=tuple(results),
        )


async def run_observations_v3(
    *,
    protocol: EvaluationProtocolV3,
    schedule: Sequence[ScheduledTurnV3],
    checkpoint_path: Path,
    cases: Mapping[str, EvaluationCaseV3],
    executor_factory: ObservationExecutorFactoryV3,
) -> EvaluationRunResultV3:
    return await EvaluationV3ObservationRunner(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=checkpoint_path,
        cases=cases,
        executor_factory=executor_factory,
    ).run()


run_evaluation_v3 = run_observations_v3


def execution_namespace_v3(
    *,
    run_id: str,
    protocol_sha256: str,
    execution_case_sha256: str,
    execution_case_set_sha256: str,
    schedule_sha256: str,
    canonical_turn_id: str,
    observation_id: str | None,
) -> ExecutionNamespaceV3:
    digest = canonical_sha256(
        {
            "canonical_turn_id": canonical_turn_id,
            "execution_case_set_sha256": execution_case_set_sha256,
            "execution_case_sha256": execution_case_sha256,
            "observation_id": observation_id,
            "protocol_sha256": protocol_sha256,
            "run_id": run_id,
            "schedule_sha256": schedule_sha256,
            "schema_version": "3.0",
        }
    )
    return ExecutionNamespaceV3(
        namespace_id=f"namespace_{digest}",
        tenant_id=f"tenant_{digest}",
        principal_id=f"principal_{digest}",
        conversation_id=f"conversation_{digest}",
    )


def execution_attribution_v3(
    context: ObservationExecutionContextV3,
    user_turn: EvaluationUserTurnV3,
) -> ExecutionAttributionV3:
    execution_turn_id = "eturn_" + canonical_sha256(
        {
            "canonical_turn_id": context.canonical_turn_id,
            "execution_case_sha256": context.execution_case_sha256,
            "source_turn_id": user_turn.source_turn_id,
            "turn_ordinal": user_turn.ordinal,
        }
    )
    return ExecutionAttributionV3(
        run_id=context.run_id,
        canonical_turn_id=context.canonical_turn_id,
        observation_id=context.observation_id,
        execution_turn_id=execution_turn_id,
        source_turn_id=user_turn.source_turn_id,
    )


def initial_state_reset_receipt_v3(
    context: ObservationExecutionContextV3,
    case: EvaluationCaseV3,
) -> InitialStateResetReceiptV3:
    fixture = case.sandbox_fixture
    execution_case_sha256 = execution_case_sha256_v3(case)
    if execution_case_sha256 != context.execution_case_sha256:
        raise ValueError("reset case differs from the execution context")
    descriptor: dict[str, JsonValue] = {
        "case_id": case.case_id,
        "canonical_turn_id": context.canonical_turn_id,
        "execution_case_set_sha256": context.execution_case_set_sha256,
        "execution_case_sha256": execution_case_sha256,
        "initial_state_sha256": canonical_sha256(case.initial_state),
        "namespace_id": context.namespace.namespace_id,
        "observation_id": context.observation_id,
        "run_id": context.run_id,
        "sandbox_fixture_id": fixture.fixture_id if fixture is not None else None,
        "sandbox_fixture_sha256": (
            fixture.fixture_sha256 if fixture is not None else None
        ),
        "schema_version": "3.0",
        "work_group_id": case.work_group_id,
    }
    return InitialStateResetReceiptV3(
        reset_id=f"reset_{canonical_sha256(descriptor)}",
        run_id=context.run_id,
        execution_case_sha256=execution_case_sha256,
        execution_case_set_sha256=context.execution_case_set_sha256,
        canonical_turn_id=context.canonical_turn_id,
        observation_id=context.observation_id,
        namespace_id=context.namespace.namespace_id,
        case_id=case.case_id,
        work_group_id=case.work_group_id,
        initial_state_sha256=canonical_sha256(case.initial_state),
        sandbox_fixture_id=fixture.fixture_id if fixture is not None else None,
        sandbox_fixture_sha256=(
            fixture.fixture_sha256 if fixture is not None else None
        ),
        reset_revision=fixture.reset_revision if fixture is not None else None,
    )


def _execution_context(
    turn: ScheduledTurnV3,
    *,
    execution_case_sha256: str,
    execution_case_set_sha256: str,
    schedule_sha256: str,
    limits: EvaluationResourceLimitsV3,
) -> ObservationExecutionContextV3:
    namespace = execution_namespace_v3(
        run_id=turn.identity.run_id,
        protocol_sha256=turn.identity.protocol_sha256,
        execution_case_sha256=execution_case_sha256,
        execution_case_set_sha256=execution_case_set_sha256,
        schedule_sha256=schedule_sha256,
        canonical_turn_id=turn.turn_id,
        observation_id=turn.observation_id,
    )
    return ObservationExecutionContextV3(
        run_id=turn.identity.run_id,
        protocol_sha256=turn.identity.protocol_sha256,
        execution_case_sha256=execution_case_sha256,
        execution_case_set_sha256=execution_case_set_sha256,
        schedule_sha256=schedule_sha256,
        schedule_index=turn.schedule_index,
        canonical_turn_id=turn.turn_id,
        observation_id=turn.observation_id,
        identity=turn.identity,
        namespace=namespace,
        limits=limits,
    )


def _turn_request(
    context: ObservationExecutionContextV3,
    case: EvaluationCaseV3,
    user_turn: EvaluationUserTurnV3,
) -> UserTurnExecutionRequestV3:
    attribution = execution_attribution_v3(context, user_turn)
    client_turn_id, request_id, trace_id = _request_ids(context, user_turn)
    return UserTurnExecutionRequestV3(
        context=context,
        case_id=case.case_id,
        work_group_id=case.work_group_id,
        principal_role=case.principal_role,
        scopes=case.scopes,
        user_turn=user_turn,
        attribution=attribution,
        client_turn_id=client_turn_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def _request_ids(
    context: ObservationExecutionContextV3,
    user_turn: EvaluationUserTurnV3,
) -> tuple[str, str, str]:
    per_turn = canonical_sha256(
        {
            "canonical_turn_id": context.canonical_turn_id,
            "execution_case_sha256": context.execution_case_sha256,
            "source_turn_id": user_turn.source_turn_id,
            "turn_ordinal": user_turn.ordinal,
        }
    )
    trace = canonical_sha256(
        {
            "canonical_turn_id": context.canonical_turn_id,
            "execution_case_set_sha256": context.execution_case_set_sha256,
            "execution_case_sha256": context.execution_case_sha256,
            "run_id": context.run_id,
            "schedule_sha256": context.schedule_sha256,
        }
    )
    return f"client_{per_turn}", f"request_{per_turn}", f"trace_{trace}"


def _validate_turn_result(
    result: UserTurnExecutionResultV3,
    request: UserTurnExecutionRequestV3,
) -> None:
    if result.attribution != request.attribution:
        raise ObservationExecutionFailureV3("executor_attribution_invalid")


def _enforce_resource_limits(
    results: Sequence[UserTurnExecutionResultV3],
    limits: EvaluationResourceLimitsV3,
) -> None:
    for result in results:
        if result.generation_calls > limits.max_generation_calls:
            raise ObservationResourceLimitErrorV3("generation_call_limit_exceeded")
        if result.provider_attempts > limits.max_provider_attempts:
            raise ObservationResourceLimitErrorV3("provider_attempt_limit_exceeded")
        if result.peak_provider_concurrency > limits.provider_concurrency:
            raise ObservationResourceLimitErrorV3("provider_concurrency_limit_exceeded")
        effective_cost = result.known_cost_usd + result.unresolved_reserved_cost_usd
        if effective_cost > limits.per_turn_limit_usd:
            raise ObservationResourceLimitErrorV3("per_turn_cost_limit_exceeded")
        if result.max_attempt_duration_seconds > limits.attempt_timeout_seconds:
            raise ObservationResourceLimitErrorV3("attempt_timeout_limit_exceeded")
        if result.elapsed_seconds > limits.turn_deadline_seconds:
            raise ObservationResourceLimitErrorV3("turn_deadline_limit_exceeded")
        for model_call in result.model_calls:
            if max(0, model_call.attempts - 1) > limits.max_retries:
                raise ObservationResourceLimitErrorV3("provider_retry_limit_exceeded")
        for embedding_call in result.embedding_calls:
            if max(0, embedding_call.attempts - 1) > limits.max_retries:
                raise ObservationResourceLimitErrorV3("provider_retry_limit_exceeded")
        for model_call in result.model_calls:
            if model_call.input_tokens > limits.max_input_tokens_per_generation:
                raise ObservationResourceLimitErrorV3(
                    "generation_input_token_limit_exceeded"
                )
            if model_call.output_tokens > limits.max_output_tokens_per_generation:
                raise ObservationResourceLimitErrorV3(
                    "generation_output_token_limit_exceeded"
                )


def _resource_totals(
    results: Sequence[UserTurnExecutionResultV3],
) -> ObservationResourceTotalsV3:
    return ObservationResourceTotalsV3(
        generation_calls=sum(result.generation_calls for result in results),
        provider_attempts=sum(result.provider_attempts for result in results),
        retry_count=sum(len(result.retry_events) for result in results),
        peak_provider_concurrency=max(
            (result.peak_provider_concurrency for result in results),
            default=0,
        ),
        input_tokens=sum(result.input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        known_cost_usd=sum(
            (result.known_cost_usd for result in results),
            Decimal("0"),
        ),
        unresolved_reserved_cost_usd=sum(
            (result.unresolved_reserved_cost_usd for result in results),
            Decimal("0"),
        ),
    )


def _pilot_observation_cost_evidence(
    receipt: ObservationRunReceiptV3,
) -> PilotObservationCostEvidenceV3:
    pilot_cost = receipt.pilot_cost
    if pilot_cost is None:
        raise ValueError("receipt has no pilot cost evidence")
    return PilotObservationCostEvidenceV3(
        pilot_cost=pilot_cost,
        user_turn_costs=tuple(
            PilotUserTurnCostEvidenceV3(
                canonical_turn_id=receipt.canonical_turn_id,
                execution_turn_id=result.attribution.execution_turn_id,
                known_cost_usd=result.known_cost_usd,
                unresolved_reservation_maxima_usd=tuple(
                    event.unresolved_reserved_maximum_usd
                    for event in result.ledger_events
                    if event.unresolved_reserved_maximum_usd > 0
                ),
                ledger_event_ids=tuple(
                    event.ledger_event_id for event in result.ledger_events
                ),
            )
            for result in receipt.turn_results
        ),
    )


def _build_receipt(
    turn: ScheduledTurnV3,
    *,
    context: ObservationExecutionContextV3,
    terminal_status: ObservationTerminalStatusV3,
    safe_error_code: str | None,
    initial_state_reset: bool,
    initial_state_reset_receipt: InitialStateResetReceiptV3 | None,
    results: tuple[UserTurnExecutionResultV3, ...],
) -> ObservationRunReceiptV3:
    totals = _resource_totals(results)
    ledger_events = tuple(event for result in results for event in result.ledger_events)
    pilot_cost = None
    if ledger_events:
        ledger_ids = tuple(event.ledger_event_id for event in ledger_events)
        if len(ledger_ids) != len(set(ledger_ids)):
            raise CheckpointIntegrityErrorV3(
                "executor reused ledger event IDs within one observation"
            )
        pilot_cost = PilotTurnCostV3(
            turn_id=turn.turn_id,
            identity=turn.identity,
            variant_id=turn.identity.variant_id,
            case_id=turn.identity.case_id,
            is_warmup=(turn.identity.turn_kind is ScheduledTurnKindV3.WARMUP),
            known_cost_usd=totals.known_cost_usd,
            unresolved_reservation_maxima_usd=tuple(
                event.unresolved_reserved_maximum_usd
                for event in ledger_events
                if event.unresolved_reserved_maximum_usd > 0
            ),
            ledger_attributed=True,
            ledger_valid=True,
            ledger_event_ids=ledger_ids,
        )
    if terminal_status is ObservationTerminalStatusV3.COMPLETED and pilot_cost is None:
        raise CheckpointIntegrityErrorV3(
            "completed observation omitted authoritative ledger evidence"
        )
    return ObservationRunReceiptV3(
        terminal_status=terminal_status,
        safe_error_code=safe_error_code,
        run_id=turn.identity.run_id,
        protocol_sha256=turn.identity.protocol_sha256,
        execution_case_sha256=context.execution_case_sha256,
        execution_case_set_sha256=context.execution_case_set_sha256,
        schedule_sha256=context.schedule_sha256,
        schedule_index=turn.schedule_index,
        execution_order=turn.execution_order,
        canonical_turn_id=turn.turn_id,
        observation_id=turn.observation_id,
        identity=turn.identity,
        namespace=context.namespace,
        limits=context.limits,
        initial_state_reset=initial_state_reset,
        initial_state_reset_receipt=initial_state_reset_receipt,
        turn_results=results,
        totals=totals,
        pilot_cost=pilot_cost,
    )


def _load_terminal_receipts(
    state: CheckpointStateV3,
    *,
    schedule: Sequence[ScheduledTurnV3],
    schedule_sha256: str,
    cases: Mapping[str, EvaluationCaseV3],
    execution_case_sha256_by_id: Mapping[str, str],
    execution_case_set_sha256: str,
    limits: EvaluationResourceLimitsV3,
) -> tuple[ObservationRunReceiptV3, ...]:
    turns = {turn.turn_id: turn for turn in schedule}
    receipts: list[ObservationRunReceiptV3] = []
    for record in state.records:
        if record.event is CheckpointEventV3.STARTED:
            continue
        if record.event is CheckpointEventV3.AMBIGUOUS:
            _validate_ambiguity_record(
                record.payload.get("ambiguity"),
                record_turn_id=record.turn_id,
                turns=turns,
                schedule_sha256=schedule_sha256,
                execution_case_sha256_by_id=execution_case_sha256_by_id,
                execution_case_set_sha256=execution_case_set_sha256,
            )
            continue
        raw_receipt = record.payload.get("receipt")
        try:
            receipt = ObservationRunReceiptV3.model_validate(raw_receipt)
        except Exception as exc:
            raise CheckpointIntegrityErrorV3(
                "checkpoint terminal receipt is invalid"
            ) from exc
        expected_event = (
            CheckpointEventV3.COMPLETED
            if receipt.terminal_status is ObservationTerminalStatusV3.COMPLETED
            else CheckpointEventV3.FAILED
        )
        if record.event is not expected_event:
            raise CheckpointIntegrityErrorV3(
                "checkpoint event and terminal receipt status disagree"
            )
        turn = turns[record.turn_id]
        if (
            receipt.schedule_sha256 != schedule_sha256
            or receipt.execution_case_set_sha256 != execution_case_set_sha256
            or receipt.execution_case_sha256
            != execution_case_sha256_by_id[turn.identity.case_id]
            or receipt.schedule_index != turn.schedule_index
            or receipt.execution_order != turn.execution_order
            or receipt.identity != turn.identity
            or receipt.canonical_turn_id != turn.turn_id
            or receipt.observation_id != turn.observation_id
            or receipt.limits != limits
        ):
            raise CheckpointIntegrityErrorV3(
                "checkpoint receipt differs from the current frozen schedule"
            )
        case = cases[turn.identity.case_id]
        expected_source_ids = tuple(item.source_turn_id for item in case.user_turns)
        actual_source_ids = tuple(
            item.attribution.source_turn_id for item in receipt.turn_results
        )
        if (
            receipt.terminal_status is ObservationTerminalStatusV3.COMPLETED
            and actual_source_ids != expected_source_ids
        ):
            raise CheckpointIntegrityErrorV3(
                "completed checkpoint receipt omitted a case user turn"
            )
        expected_reset = initial_state_reset_receipt_v3(
            _execution_context(
                turn,
                execution_case_sha256=execution_case_sha256_by_id[
                    turn.identity.case_id
                ],
                execution_case_set_sha256=execution_case_set_sha256,
                schedule_sha256=receipt.schedule_sha256,
                limits=receipt.limits,
            ),
            case,
        )
        if receipt.initial_state_reset_receipt is not None and (
            receipt.initial_state_reset_receipt != expected_reset
        ):
            raise CheckpointIntegrityErrorV3(
                "checkpoint reset acknowledgment differs from the case fixture"
            )
        context = _execution_context(
            turn,
            execution_case_sha256=execution_case_sha256_by_id[turn.identity.case_id],
            execution_case_set_sha256=execution_case_set_sha256,
            schedule_sha256=receipt.schedule_sha256,
            limits=receipt.limits,
        )
        try:
            _validate_observation_results(
                receipt.turn_results,
                context=context,
                case=case,
            )
            if receipt.terminal_status is ObservationTerminalStatusV3.COMPLETED:
                _enforce_resource_limits(receipt.turn_results, limits)
        except (ObservationExecutionFailureV3, ObservationResourceLimitErrorV3) as exc:
            raise CheckpointIntegrityErrorV3(
                "checkpoint receipt contains invalid executor evidence"
            ) from exc
        receipts.append(receipt)
    return tuple(receipts)


def _validate_ambiguity_record(
    raw: JsonValue | None,
    *,
    record_turn_id: str,
    turns: Mapping[str, ScheduledTurnV3],
    schedule_sha256: str,
    execution_case_sha256_by_id: Mapping[str, str],
    execution_case_set_sha256: str,
) -> None:
    try:
        ambiguity = AmbiguousObservationReceiptV3.model_validate(raw)
    except Exception as exc:
        raise CheckpointIntegrityErrorV3(
            "checkpoint ambiguity receipt is invalid"
        ) from exc
    turn = turns[record_turn_id]
    if (
        ambiguity.run_id != turn.identity.run_id
        or ambiguity.protocol_sha256 != turn.identity.protocol_sha256
        or ambiguity.execution_case_set_sha256 != execution_case_set_sha256
        or ambiguity.execution_case_sha256
        != execution_case_sha256_by_id[turn.identity.case_id]
        or ambiguity.schedule_sha256 != schedule_sha256
        or ambiguity.schedule_index != turn.schedule_index
        or ambiguity.canonical_turn_id != turn.turn_id
        or ambiguity.observation_id != turn.observation_id
    ):
        raise CheckpointIntegrityErrorV3(
            "checkpoint ambiguity receipt differs from the frozen schedule"
        )


def _validate_observation_results(
    results: Sequence[UserTurnExecutionResultV3],
    *,
    context: ObservationExecutionContextV3,
    case: EvaluationCaseV3,
) -> None:
    expected_turns = case.user_turns[: len(results)]
    actual_source_ids = tuple(result.attribution.source_turn_id for result in results)
    expected_source_ids = tuple(item.source_turn_id for item in expected_turns)
    if actual_source_ids != expected_source_ids:
        raise ObservationExecutionFailureV3("executor_turn_sequence_invalid")
    for result, user_turn in zip(results, expected_turns, strict=True):
        if result.attribution != execution_attribution_v3(context, user_turn):
            raise ObservationExecutionFailureV3("executor_attribution_invalid")
    identifier_groups = (
        tuple(result.attribution.execution_turn_id for result in results),
        tuple(call.call_id for result in results for call in result.model_calls)
        + tuple(call.call_id for result in results for call in result.embedding_calls),
        tuple(
            event.retry_event_id for result in results for event in result.retry_events
        ),
        tuple(
            event.ledger_event_id
            for result in results
            for event in result.ledger_events
        ),
    )
    if any(len(values) != len(set(values)) for values in identifier_groups):
        raise ObservationExecutionFailureV3("executor_evidence_id_reused")


def _run_result(
    state: CheckpointStateV3,
    *,
    schedule: Sequence[ScheduledTurnV3],
    run_id: str,
    protocol_sha256: str,
    execution_case_set_sha256: str,
    schedule_sha256: str,
    cases: Mapping[str, EvaluationCaseV3],
    execution_case_sha256_by_id: Mapping[str, str],
    limits: EvaluationResourceLimitsV3,
) -> EvaluationRunResultV3:
    receipts = _load_terminal_receipts(
        state,
        schedule=schedule,
        schedule_sha256=schedule_sha256,
        cases=cases,
        execution_case_sha256_by_id=execution_case_sha256_by_id,
        execution_case_set_sha256=execution_case_set_sha256,
        limits=limits,
    )
    completed_set = set(state.completed_turn_ids)
    completed_observations = tuple(
        turn.observation_id
        for turn in schedule
        if turn.turn_id in completed_set and turn.observation_id is not None
    )
    summary = EvaluationRunSummaryV3(
        run_id=run_id,
        protocol_sha256=protocol_sha256,
        execution_case_set_sha256=execution_case_set_sha256,
        schedule_sha256=schedule_sha256,
        total_scheduled=len(schedule),
        completed_turn_ids=state.completed_turn_ids,
        failed_turn_ids=state.failed_turn_ids,
        pending_turn_ids=state.pending_turn_ids,
        missing_turn_ids=state.missing_turn_ids,
        ambiguous_turn_ids=state.ambiguous_turn_ids,
        orphan_started_turn_ids=state.orphan_started_turn_ids,
        completed_observation_ids=completed_observations,
        is_partial=state.partial,
        blocked_on_ambiguous_work=bool(state.ambiguous_turn_ids),
    )
    return EvaluationRunResultV3(summary=summary, receipts=receipts)


def _validate_cases(
    protocol: EvaluationProtocolV3,
    schedule: Sequence[ScheduledTurnV3],
    cases: Mapping[str, EvaluationCaseV3],
) -> dict[str, EvaluationCaseV3]:
    expected_groups: dict[str, str] = {
        item.case_id: item.work_group_id for item in protocol.experiment.pilot_cases
    }
    required_ids = {turn.identity.case_id for turn in schedule}
    supplied_ids = set(cases)
    missing = sorted(required_ids - supplied_ids)
    if missing:
        raise ValueError(f"runner case adapters are missing: {missing}")
    materialized: dict[str, EvaluationCaseV3] = {}
    for case_id in sorted(required_ids):
        case = EvaluationCaseV3.model_validate(cases[case_id])
        if case.case_id != case_id:
            raise ValueError("case adapter key and case ID disagree")
        if expected_groups.get(case_id) != case.work_group_id:
            raise ValueError("case adapter work group differs from the protocol")
        materialized[case_id] = case
    return materialized


def _validate_schedule_against_protocol(
    protocol: EvaluationProtocolV3,
    schedule: Sequence[ScheduledTurnV3],
    *,
    run_id: str,
    protocol_sha256: str,
) -> None:
    if not schedule:
        raise ValueError("evaluation schedule cannot be empty")
    variant_ids = {variant.variant_id for variant in protocol.variants}
    case_groups: dict[str, str] = {
        case.case_id: case.work_group_id for case in protocol.experiment.pilot_cases
    }
    if [turn.schedule_index for turn in schedule] != list(range(len(schedule))):
        raise ValueError("schedule indices must be contiguous from zero")
    measured_orders = [
        turn.execution_order
        for turn in schedule
        if turn.identity.turn_kind is ScheduledTurnKindV3.MEASURED
    ]
    if measured_orders != list(range(len(measured_orders))):
        raise ValueError("measured execution order must be contiguous from zero")
    for turn in schedule:
        identity = turn.identity
        if identity.run_id != run_id:
            raise ValueError("schedule contains more than one run ID")
        if identity.protocol_sha256 != protocol_sha256:
            raise ValueError("schedule protocol hash differs from the frozen protocol")
        if identity.variant_id not in variant_ids:
            raise ValueError("schedule references a foreign variant")
        if case_groups.get(identity.case_id) != identity.work_group_id:
            raise ValueError("schedule case and work group differ from the protocol")
        if (
            identity.turn_kind is ScheduledTurnKindV3.WARMUP
            and identity.case_id != protocol.experiment.warmup_case_id
        ):
            raise ValueError("warmup schedule item uses a non-warmup case")


def _schedule_run_id(schedule: Sequence[ScheduledTurnV3]) -> str:
    run_ids = {turn.identity.run_id for turn in schedule}
    if len(run_ids) != 1:
        raise ValueError("evaluation schedule must contain exactly one run ID")
    return next(iter(run_ids))


def _merge_partial_results(
    results: list[UserTurnExecutionResultV3],
    partial_results: Sequence[UserTurnExecutionResultV3],
    *,
    context: ObservationExecutionContextV3,
    case: EvaluationCaseV3,
) -> None:
    existing = {result.attribution.execution_turn_id: result for result in results}
    for result in partial_results:
        prior = existing.get(result.attribution.execution_turn_id)
        if prior is not None:
            if prior != result:
                raise ObservationExecutionFailureV3(
                    "executor_partial_evidence_conflict"
                )
            continue
        candidate_results = (*results, result)
        _validate_observation_results(
            candidate_results,
            context=context,
            case=case,
        )
        results.append(result)
        existing[result.attribution.execution_turn_id] = result


def _canonical_turn_id(identity: ObservationIdentityV3) -> str:
    return f"turn_{canonical_sha256(identity)}"


def _canonical_observation_id(identity: ObservationIdentityV3) -> str:
    return f"obs_{canonical_sha256(identity)}"


__all__ = [
    "EmbeddingCallEvidenceV3",
    "AmbiguousObservationReceiptV3",
    "EvaluationCaseV3",
    "EvaluationResourceLimitsV3",
    "EvaluationRunResultV3",
    "EvaluationRunSummaryV3",
    "EvaluationUserTurnV3",
    "EvaluationV3ObservationRunner",
    "ExecutionAttributionV3",
    "ExecutionNamespaceV3",
    "InitialStateResetReceiptV3",
    "LedgerEventEvidenceV3",
    "LedgerEventKindV3",
    "ModelCallEvidenceV3",
    "ObservationExecutionContextV3",
    "ObservationExecutionFailureV3",
    "ObservationExecutorFactoryV3",
    "ObservationExecutorV3",
    "ObservationResourceLimitErrorV3",
    "ObservationResourceTotalsV3",
    "ObservationRunReceiptV3",
    "ObservationTerminalStatusV3",
    "RetryEvidenceV3",
    "SandboxFixtureAdapterV3",
    "UserTurnExecutionRequestV3",
    "UserTurnExecutionResultV3",
    "execution_case_set_sha256_v3",
    "execution_case_sha256_v3",
    "execution_attribution_v3",
    "execution_namespace_v3",
    "initial_state_reset_receipt_v3",
    "run_evaluation_v3",
    "run_observations_v3",
]
