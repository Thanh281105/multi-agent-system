"""Strict, provider-neutral contracts for the version 2 conversation API.

These models intentionally contain safe, user-facing summaries rather than raw
model prompts, reasoning traces, or tool request/response payloads.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.contracts import TaskStatus

MAX_MESSAGE_LENGTH = 2_000
MAX_PLAN_REVISIONS = 1
MAX_ADDED_READ_STEPS = 2
MAX_EXPERT_STEPS = 8
MAX_CANDIDATES = 5
MAX_KNOWLEDGE_RETRIEVALS = 2
MAX_DRAFT_REPAIRS = 1

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
CLIENT_TURN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
ACTION_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
DISPLAY_CITATION_PATTERN = r"^\[C[1-9][0-9]*\]$"

PriceVnd = Annotated[int, Field(strict=True, gt=0, le=10_000_000_000)]
ProductId = Annotated[int, Field(strict=True, gt=0)]
Quantity = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
PositiveQuantity = Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
ResourceVersion = Annotated[int, Field(strict=True, ge=1)]
StableId = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]


class V2Contract(BaseModel):
    """Base for immutable contracts that reject undeclared input fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationMode(StrEnum):
    SHOPPER = "shopper"
    MERCHANT = "merchant"


class DialogueOutcome(StrEnum):
    ANSWERED = "answered"
    NEEDS_CLARIFICATION = "needs_clarification"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ABSTAINED = "abstained"


class TurnStatus(StrEnum):
    """Durable v2 lifecycle, kept separate from the v1 execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class EvidenceKind(StrEnum):
    CATALOG = "catalog"
    REVIEW = "review"
    TRUST = "trust"
    MARKET = "market"
    KNOWLEDGE = "knowledge"
    SANDBOX = "sandbox"


class ActionKind(StrEnum):
    CART_CHANGE = "cart_change"
    CHECKOUT = "checkout"
    MERCHANT_PRICE_CHANGE = "merchant_price_change"
    MERCHANT_INVENTORY_CHANGE = "merchant_inventory_change"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"
    CONFLICTED = "conflicted"
    FAILED = "failed"


class PreferenceKind(StrEnum):
    GENRE = "genre"
    AUTHOR = "author"
    LANGUAGE = "language"
    MAX_BUDGET_VND = "max_budget_vnd"


class MeResponse(V2Contract):
    principal_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    allowed_modes: tuple[ConversationMode, ...]
    store_id: str = Field(pattern=IDENTIFIER_PATTERN)

    @field_validator("allowed_modes")
    @classmethod
    def validate_allowed_modes(
        cls, value: tuple[ConversationMode, ...]
    ) -> tuple[ConversationMode, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed modes must be unique")
        return value


class ConversationCreateRequest(V2Contract):
    """Client-selected mode; identity, role, tenant, and store stay server-owned."""

    mode: ConversationMode


class ConversationSummary(V2Contract):
    conversation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    mode: ConversationMode
    store_id: str = Field(pattern=IDENTIFIER_PATTERN)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_timestamps(self) -> ConversationSummary:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class ConversationCreateResponse(V2Contract):
    conversation: ConversationSummary


class ConversationListResponse(V2Contract):
    conversations: tuple[ConversationSummary, ...]
    next_cursor: str | None = Field(default=None, min_length=1, max_length=256)


class ChatRequest(V2Contract):
    conversation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    client_turn_id: str = Field(pattern=CLIENT_TURN_ID_PATTERN)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must contain non-whitespace text")
        return value


class SafeExecutionError(V2Contract):
    code: str = Field(pattern=ACTION_PATTERN)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False


class ExecutionRecord(V2Contract):
    execution_id: str = Field(pattern=IDENTIFIER_PATTERN)
    step_id: str = Field(pattern=IDENTIFIER_PATTERN)
    operation_key: str = Field(min_length=8, max_length=256)
    capability: str = Field(pattern=ACTION_PATTERN)
    service: str = Field(pattern=IDENTIFIER_PATTERN)
    status: TaskStatus
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    duration_ms: float = Field(default=0, ge=0)
    reused: bool = False
    error: SafeExecutionError | None = None

    @model_validator(mode="after")
    def validate_execution(self) -> ExecutionRecord:
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at cannot precede started_at")
        if self.status == TaskStatus.FAILED and self.error is None:
            raise ValueError("failed execution must include a safe error")
        if self.status == TaskStatus.SUCCESS and self.error is not None:
            raise ValueError("successful execution cannot include an error")
        return self


class PlanStep(V2Contract):
    """Safe plan metadata; validated tool parameters remain server-side."""

    step_id: str = Field(pattern=IDENTIFIER_PATTERN)
    operation_key: str = Field(min_length=8, max_length=256)
    capability: str = Field(pattern=ACTION_PATTERN)
    service: str = Field(pattern=IDENTIFIER_PATTERN)
    depends_on: tuple[StableId, ...] = ()
    data_version_ids: tuple[StableId, ...] = ()

    @field_validator("depends_on", "data_version_ids")
    @classmethod
    def validate_unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("referenced IDs must be unique")
        return value


class PlanRevision(V2Contract):
    revision: int = Field(strict=True, ge=0, le=MAX_PLAN_REVISIONS)
    reason: str = Field(min_length=1, max_length=300)
    steps: tuple[PlanStep, ...] = Field(max_length=MAX_EXPERT_STEPS)
    added_read_step_ids: tuple[StableId, ...] = Field(
        default=(), max_length=MAX_ADDED_READ_STEPS
    )
    executed_step_ids: tuple[StableId, ...] = ()
    reused_step_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_step_references(self) -> PlanRevision:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step IDs must be unique")
        known = set(step_ids)
        for name, references in (
            ("added read", self.added_read_step_ids),
            ("executed", self.executed_step_ids),
            ("reused", self.reused_step_ids),
        ):
            if len(references) != len(set(references)):
                raise ValueError(f"{name} step IDs must be unique")
            if not set(references) <= known:
                raise ValueError(f"{name} step IDs must reference plan steps")
        if set(self.executed_step_ids) & set(self.reused_step_ids):
            raise ValueError("executed and reused step IDs must be disjoint")

        seen: set[str] = set()
        for step in self.steps:
            if not set(step.depends_on) <= seen:
                raise ValueError(f"step {step.step_id} has unresolved dependencies")
            seen.add(step.step_id)
        return self


class PlanTrace(V2Contract):
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    intent: str = Field(pattern=ACTION_PATTERN)
    revisions: tuple[PlanRevision, ...] = Field(
        min_length=1, max_length=MAX_PLAN_REVISIONS + 1
    )

    @model_validator(mode="after")
    def validate_revisions(self) -> PlanTrace:
        expected = tuple(range(len(self.revisions)))
        actual = tuple(revision.revision for revision in self.revisions)
        if actual != expected:
            raise ValueError("plan revisions must be consecutive and start at zero")

        step_definitions: dict[
            str, tuple[str, str, str, tuple[str, ...], tuple[str, ...]]
        ] = {}
        operation_owners: dict[str, str] = {}
        executed_once: set[str] = set()
        prior_step_ids: set[str] = set()
        for revision_index, revision in enumerate(self.revisions):
            current_step_ids = {step.step_id for step in revision.steps}
            added_read_step_ids = set(revision.added_read_step_ids)
            if revision_index == 0 and added_read_step_ids:
                raise ValueError("the initial plan cannot contain added read steps")
            if revision_index > 0:
                if not prior_step_ids <= current_step_ids:
                    raise ValueError("a plan revision cannot remove earlier steps")
                new_step_ids = current_step_ids - prior_step_ids
                if new_step_ids != added_read_step_ids:
                    raise ValueError(
                        "continuation steps must exactly match added read step IDs"
                    )
            for step in revision.steps:
                definition = (
                    step.operation_key,
                    step.capability,
                    step.service,
                    step.depends_on,
                    step.data_version_ids,
                )
                previous = step_definitions.setdefault(step.step_id, definition)
                if previous != definition:
                    raise ValueError("a reused step ID cannot change its definition")
                owner = operation_owners.setdefault(step.operation_key, step.step_id)
                if owner != step.step_id:
                    raise ValueError("an operation key cannot identify multiple steps")
            if set(revision.executed_step_ids) & executed_once:
                raise ValueError("an executed step cannot be dispatched twice")
            if not set(revision.reused_step_ids) <= executed_once:
                raise ValueError(
                    "reused steps must have executed in an earlier revision"
                )
            executed_once.update(revision.executed_step_ids)
            prior_step_ids.update(current_step_ids)

        if len(step_definitions) > MAX_EXPERT_STEPS:
            raise ValueError("plan exceeds the expert step limit")
        added_reads = {
            step_id
            for revision in self.revisions
            for step_id in revision.added_read_step_ids
        }
        if len(added_reads) > MAX_ADDED_READ_STEPS:
            raise ValueError("plan exceeds the added read step limit")
        knowledge_steps = {
            step.step_id
            for revision in self.revisions
            for step in revision.steps
            if step.capability == "knowledge.retrieve"
        }
        if len(knowledge_steps) > MAX_KNOWLEDGE_RETRIEVALS:
            raise ValueError("plan exceeds the knowledge retrieval limit")
        return self


class EvidenceReference(V2Contract):
    """Stable evidence identity plus a separate, presentation-only label."""

    evidence_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    chunk_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    span_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    display_label: str = Field(pattern=DISPLAY_CITATION_PATTERN)
    kind: EvidenceKind
    title: str = Field(min_length=1, max_length=300)
    url: HttpUrl | None = None
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_span_location(self) -> EvidenceReference:
        if self.span_id is not None and self.chunk_id is None:
            raise ValueError("span evidence must identify its chunk")
        return self


class Citation(V2Contract):
    citation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    claim_id: str = Field(pattern=IDENTIFIER_PATTERN)
    evidence_id: str = Field(pattern=IDENTIFIER_PATTERN)
    span_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    display_label: str = Field(pattern=DISPLAY_CITATION_PATTERN)


class Claim(V2Contract):
    claim_id: str = Field(pattern=IDENTIFIER_PATTERN)
    text: str = Field(min_length=1, max_length=1_000)
    citation_ids: tuple[StableId, ...] = Field(min_length=1)

    @field_validator("citation_ids")
    @classmethod
    def validate_unique_citations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("claim citation IDs must be unique")
        return value


class ActionChange(V2Contract):
    resource_type: Literal["cart_item", "order", "offer"]
    resource_id: str = Field(pattern=IDENTIFIER_PATTERN)
    field: Literal["quantity", "price_vnd", "status"]
    before_integer: int | None = Field(default=None, strict=True, ge=0)
    after_integer: int | None = Field(default=None, strict=True, ge=0)
    before_text: str | None = Field(default=None, min_length=1, max_length=80)
    after_text: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_change_values(self) -> ActionChange:
        before_count = int(self.before_integer is not None) + int(
            self.before_text is not None
        )
        after_count = int(self.after_integer is not None) + int(
            self.after_text is not None
        )
        if before_count > 1 or after_count != 1:
            raise ValueError("action change must have one typed after value")
        if self.field in {"quantity", "price_vnd"} and self.after_integer is None:
            raise ValueError("numeric action fields require integer values")
        if self.field == "status" and self.after_text is None:
            raise ValueError("status action fields require text values")
        if self.field == "price_vnd" and self.after_integer == 0:
            raise ValueError("price_vnd must be positive")
        return self


class ActionTarget(V2Contract):
    resource_type: Literal["cart", "order", "offer"]
    resource_id: str = Field(pattern=IDENTIFIER_PATTERN)
    expected_resource_version: ResourceVersion
    data_version_ids: tuple[StableId, ...] = Field(min_length=1)

    @field_validator("data_version_ids")
    @classmethod
    def validate_data_versions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("action data version IDs must be unique")
        return value


class ActionCard(V2Contract):
    action_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposal_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposal_version: ResourceVersion
    kind: ActionKind
    status: ActionStatus
    title: str = Field(min_length=1, max_length=160)
    required_permission: str = Field(pattern=ACTION_PATTERN)
    confirmation_required: bool
    target: ActionTarget
    changes: tuple[ActionChange, ...] = Field(min_length=1)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_confirmation_policy(self) -> ActionCard:
        confirmation_required = {
            ActionKind.CHECKOUT,
            ActionKind.MERCHANT_PRICE_CHANGE,
            ActionKind.MERCHANT_INVENTORY_CHANGE,
        }
        if self.kind in confirmation_required and not self.confirmation_required:
            raise ValueError("this action kind requires explicit confirmation")
        return self


class UsageSummary(V2Contract):
    input_tokens: int = Field(default=0, strict=True, ge=0)
    cached_input_tokens: int = Field(default=0, strict=True, ge=0)
    output_tokens: int = Field(default=0, strict=True, ge=0)
    reasoning_tokens: int = Field(default=0, strict=True, ge=0)
    total_tokens: int = Field(default=0, strict=True, ge=0)
    generation_calls: int = Field(default=0, strict=True, ge=0, le=10)
    provider_attempts: int = Field(default=0, strict=True, ge=0, le=16)
    knowledge_retrievals: int = Field(
        default=0, strict=True, ge=0, le=MAX_KNOWLEDGE_RETRIEVALS
    )
    draft_repairs: int = Field(default=0, strict=True, ge=0, le=MAX_DRAFT_REPAIRS)
    estimated_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    known_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    reserved_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    unknown_reserved_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    unknown_usage_attempts: int = Field(default=0, strict=True, ge=0)
    fallback_used: bool = False

    @model_validator(mode="after")
    def validate_usage(self) -> UsageSummary:
        expected_total = self.input_tokens + self.output_tokens
        if self.total_tokens != expected_total:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens cannot exceed output tokens")
        if self.unknown_usage_attempts > self.provider_attempts:
            raise ValueError("unknown usage attempts cannot exceed provider attempts")
        expected_cost = (
            self.known_cost_usd
            + self.reserved_cost_usd
            + self.unknown_reserved_cost_usd
        )
        if self.estimated_cost_usd != expected_cost:
            raise ValueError(
                "estimated cost must equal known, pending, and unknown reservations"
            )
        return self


class TurnResult(V2Contract):
    outcome: DialogueOutcome
    answer: str = Field(min_length=1, max_length=20_000)
    claims: tuple[Claim, ...] = ()
    citations: tuple[Citation, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    action_cards: tuple[ActionCard, ...] = ()
    plan: PlanTrace | None = None
    executions: tuple[ExecutionRecord, ...] = Field(
        default=(), max_length=MAX_EXPERT_STEPS
    )
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> TurnResult:
        claim_ids = {claim.claim_id for claim in self.claims}
        citation_ids = {citation.citation_id for citation in self.citations}
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(claim_ids) != len(self.claims):
            raise ValueError("claim IDs must be unique")
        if len(citation_ids) != len(self.citations):
            raise ValueError("citation IDs must be unique")
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("evidence IDs must be unique")
        display_labels = [item.display_label for item in self.evidence]
        if len(display_labels) != len(set(display_labels)):
            raise ValueError("evidence display labels must be unique")
        citations_by_id = {
            citation.citation_id: citation for citation in self.citations
        }
        claims_by_id = {claim.claim_id: claim for claim in self.claims}
        for claim in self.claims:
            if not set(claim.citation_ids) <= citation_ids:
                raise ValueError("claim references an unknown citation")
            if any(
                citations_by_id[citation_id].claim_id != claim.claim_id
                for citation_id in claim.citation_ids
            ):
                raise ValueError("claim references a citation owned by another claim")
        for citation in self.citations:
            if citation.claim_id not in claim_ids:
                raise ValueError("citation references an unknown claim")
            if citation.citation_id not in claims_by_id[citation.claim_id].citation_ids:
                raise ValueError("citation is absent from its owning claim")
            if citation.evidence_id not in evidence_ids:
                raise ValueError("citation references unknown evidence")
            evidence = next(
                item
                for item in self.evidence
                if item.evidence_id == citation.evidence_id
            )
            if citation.span_id is not None and citation.span_id != evidence.span_id:
                raise ValueError("citation span does not match its evidence")
            if citation.display_label != evidence.display_label:
                raise ValueError("citation display label does not match its evidence")
        if (
            self.outcome == DialogueOutcome.AWAITING_CONFIRMATION
            and not self.action_cards
        ):
            raise ValueError("awaiting confirmation requires an action card")
        return self


class TurnSummary(V2Contract):
    turn_id: str = Field(pattern=IDENTIFIER_PATTERN)
    client_turn_id: str = Field(pattern=CLIENT_TURN_ID_PATTERN)
    status: TurnStatus
    outcome: DialogueOutcome | None = None
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> TurnSummary:
        terminal = {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
        }
        if self.status == TurnStatus.COMPLETED and self.outcome is None:
            raise ValueError("completed turns require a dialogue outcome")
        if self.status != TurnStatus.COMPLETED and self.outcome is not None:
            raise ValueError("only completed turns can have a dialogue outcome")
        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal turns require completed_at")
        if self.status not in terminal and self.completed_at is not None:
            raise ValueError("non-terminal turns cannot have completed_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        return self


class TurnResponse(V2Contract):
    conversation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    turn: TurnSummary
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    trace_id: str = Field(pattern=IDENTIFIER_PATTERN)
    result: TurnResult | None = None
    error: SafeExecutionError | None = None
    usage: UsageSummary = Field(default_factory=UsageSummary)

    @model_validator(mode="after")
    def validate_result_state(self) -> TurnResponse:
        if self.turn.status == TurnStatus.COMPLETED and self.result is None:
            raise ValueError("completed turns require a result")
        if self.turn.status != TurnStatus.COMPLETED and self.result is not None:
            raise ValueError("only completed turns can expose a result")
        if self.turn.status in {TurnStatus.FAILED, TurnStatus.INTERRUPTED}:
            if self.error is None:
                raise ValueError("failed or interrupted turns require a safe error")
        elif self.error is not None:
            raise ValueError("this turn state cannot expose an error")
        if self.result is not None and self.result.outcome != self.turn.outcome:
            raise ValueError("turn and result outcomes must match")
        return self


class ChatResponse(TurnResponse):
    """Terminal or in-progress response returned from the chat endpoint."""


class ConversationDetailResponse(V2Contract):
    conversation: ConversationSummary
    turns: tuple[TurnSummary, ...]


class ActionConfirmRequest(V2Contract):
    proposal_version: ResourceVersion


class ActionRejectRequest(V2Contract):
    proposal_version: ResourceVersion
    reason: str | None = Field(default=None, min_length=1, max_length=300)


class ActionDecisionResponse(V2Contract):
    action_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposal_version: ResourceVersion
    status: ActionStatus
    decided_at: AwareDatetime
    reused_result: bool = False


class GenrePreference(V2Contract):
    kind: Literal[PreferenceKind.GENRE]
    value: str = Field(min_length=1, max_length=100)


class AuthorPreference(V2Contract):
    kind: Literal[PreferenceKind.AUTHOR]
    value: str = Field(min_length=1, max_length=160)


class LanguagePreference(V2Contract):
    kind: Literal[PreferenceKind.LANGUAGE]
    value: str = Field(min_length=2, max_length=40)


class BudgetPreference(V2Contract):
    kind: Literal[PreferenceKind.MAX_BUDGET_VND]
    value: PriceVnd


PreferenceValue = Annotated[
    GenrePreference | AuthorPreference | LanguagePreference | BudgetPreference,
    Field(discriminator="kind"),
]


class PreferencePutRequest(V2Contract):
    """An explicit allowlisted preference write tied to the requesting turn."""

    source_turn_id: str = Field(pattern=IDENTIFIER_PATTERN)
    preference: PreferenceValue


class PreferenceDeleteRequest(V2Contract):
    preference_id: str = Field(pattern=IDENTIFIER_PATTERN)


class PreferenceRecord(V2Contract):
    preference_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_turn_id: str = Field(pattern=IDENTIFIER_PATTERN)
    preference: PreferenceValue
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PreferenceListResponse(V2Contract):
    preferences: tuple[PreferenceRecord, ...]
