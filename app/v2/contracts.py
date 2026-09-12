"""Strict, provider-neutral contracts for the version 2 conversation API.

These models intentionally contain safe, user-facing summaries rather than raw
model prompts, reasoning traces, or tool request/response payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    TypeAdapter,
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
MAX_ARTIFACTS = 16
MAX_ARTIFACT_ITEMS = 100
MAX_ARTIFACT_DATA_VERSIONS = 16
MAX_SSE_EVENTS = 10_000
MAX_TEXT_DELTA_LENGTH = 4_000

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
ArtifactAmountVnd = Annotated[
    int,
    Field(strict=True, ge=0, le=10_000_000_000_000_000),
]


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


class ArtifactKind(StrEnum):
    PRODUCT_COMPARISON = "product_comparison"
    CART = "cart"
    ORDER = "order"
    ACTION = "action"


class TurnSSEEventKind(StrEnum):
    PROGRESS = "progress"
    TEXT_DELTA = "text_delta"
    TERMINAL = "terminal"


class TurnSSEProgressPhase(StrEnum):
    ADMITTED = "admitted"
    ATTACHED = "attached"
    CLAIMED = "claimed"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"


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


class ArtifactProduct(V2Contract):
    """Safe catalog fields used to reopen a comparison from history."""

    product_id: ProductId
    title: str = Field(min_length=1, max_length=300)
    author: str | None = Field(default=None, min_length=1, max_length=160)
    price_vnd: PriceVnd | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    catalog_version_id: StableId


class ArtifactLineItem(V2Contract):
    """Bounded cart/order display line derived from the sandbox result."""

    product_id: ProductId
    title: str = Field(min_length=1, max_length=300)
    quantity: PositiveQuantity
    unit_price_vnd: PriceVnd
    line_total_vnd: ArtifactAmountVnd

    @model_validator(mode="after")
    def validate_line_total(self) -> ArtifactLineItem:
        if self.line_total_vnd != self.quantity * self.unit_price_vnd:
            raise ValueError("artifact line total must equal quantity times unit price")
        return self


class ArtifactBase(V2Contract):
    artifact_id: StableId
    resource_id: StableId
    resource_version: ResourceVersion
    title: str = Field(min_length=1, max_length=160)


class ProductComparisonArtifact(ArtifactBase):
    kind: Literal[ArtifactKind.PRODUCT_COMPARISON]
    status: Literal["ready", "partial"]
    products: tuple[ArtifactProduct, ...] = Field(
        min_length=2,
        max_length=MAX_CANDIDATES,
    )
    data_version_ids: tuple[StableId, ...] = Field(
        min_length=1,
        max_length=MAX_ARTIFACT_DATA_VERSIONS,
    )

    @model_validator(mode="after")
    def validate_products(self) -> ProductComparisonArtifact:
        product_ids = [product.product_id for product in self.products]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("comparison artifact product IDs must be unique")
        if len(self.data_version_ids) != len(set(self.data_version_ids)):
            raise ValueError("comparison artifact data versions must be unique")
        known_versions = set(self.data_version_ids)
        if any(
            product.catalog_version_id not in known_versions
            for product in self.products
        ):
            raise ValueError("comparison product references an unknown data version")
        return self


class CartArtifact(ArtifactBase):
    kind: Literal[ArtifactKind.CART]
    status: Literal["active", "checked_out", "abandoned"]
    items: tuple[ArtifactLineItem, ...] = Field(max_length=MAX_ARTIFACT_ITEMS)
    total_price_vnd: ArtifactAmountVnd
    data_version_ids: tuple[StableId, ...] = Field(
        min_length=1,
        max_length=MAX_ARTIFACT_DATA_VERSIONS,
    )

    @model_validator(mode="after")
    def validate_cart(self) -> CartArtifact:
        _validate_artifact_lines(self.items, self.total_price_vnd)
        _validate_unique_data_versions(self.data_version_ids)
        return self


class OrderArtifact(ArtifactBase):
    kind: Literal[ArtifactKind.ORDER]
    status: Literal["confirmed"]
    cart_id: StableId
    cart_version: ResourceVersion
    items: tuple[ArtifactLineItem, ...] = Field(
        min_length=1,
        max_length=MAX_ARTIFACT_ITEMS,
    )
    total_price_vnd: ArtifactAmountVnd
    data_version_ids: tuple[StableId, ...] = Field(
        min_length=1,
        max_length=MAX_ARTIFACT_DATA_VERSIONS,
    )
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_order(self) -> OrderArtifact:
        _validate_artifact_lines(self.items, self.total_price_vnd)
        _validate_unique_data_versions(self.data_version_ids)
        return self


class ActionArtifact(ArtifactBase):
    kind: Literal[ArtifactKind.ACTION]
    action_id: StableId
    proposal_id: StableId
    proposal_version: ResourceVersion
    action_kind: ActionKind
    status: ActionStatus


TurnArtifact = Annotated[
    ProductComparisonArtifact | CartArtifact | OrderArtifact | ActionArtifact,
    Field(discriminator="kind"),
]


def _validate_artifact_lines(
    items: tuple[ArtifactLineItem, ...],
    total_price_vnd: int,
) -> None:
    if sum(item.line_total_vnd for item in items) != total_price_vnd:
        raise ValueError("artifact total must equal the sum of its line totals")


def _validate_unique_data_versions(data_version_ids: tuple[str, ...]) -> None:
    if len(data_version_ids) != len(set(data_version_ids)):
        raise ValueError("artifact data versions must be unique")


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
    artifacts: tuple[TurnArtifact, ...] = Field(
        default=(),
        max_length=MAX_ARTIFACTS,
    )
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
        artifact_ids = {item.artifact_id for item in self.artifacts}
        if len(claim_ids) != len(self.claims):
            raise ValueError("claim IDs must be unique")
        if len(citation_ids) != len(self.citations):
            raise ValueError("citation IDs must be unique")
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("evidence IDs must be unique")
        if len(artifact_ids) != len(self.artifacts):
            raise ValueError("artifact IDs must be unique")
        action_ids = [card.action_id for card in self.action_cards]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action card IDs must be unique")
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
        action_cards_by_id = {card.action_id: card for card in self.action_cards}
        for artifact in self.artifacts:
            if not isinstance(artifact, ActionArtifact):
                continue
            card = action_cards_by_id.get(artifact.action_id)
            if card is None:
                raise ValueError("action artifact references an unknown action card")
            if (
                artifact.proposal_id != card.proposal_id
                or artifact.proposal_version != card.proposal_version
                or artifact.action_kind != card.kind
                or artifact.status != card.status
                or artifact.resource_id != card.target.resource_id
                or artifact.resource_version != card.target.expected_resource_version
                or artifact.title != card.title
            ):
                raise ValueError("action artifact does not match its action card")
        return self


class HistoryTurn(V2Contract):
    """Safe public history projection without raw runtime or tool payloads."""

    turn_id: str = Field(pattern=IDENTIFIER_PATTERN)
    client_turn_id: str = Field(pattern=CLIENT_TURN_ID_PATTERN)
    status: TurnStatus
    outcome: DialogueOutcome | None = None
    user_message: str | None = Field(default=None, max_length=MAX_MESSAGE_LENGTH)
    assistant_result: TurnResult | None = None
    error: SafeExecutionError | None = None
    action_cards: tuple[ActionCard, ...] = ()
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> HistoryTurn:
        terminal = {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
        }
        if self.status == TurnStatus.COMPLETED:
            if self.assistant_result is None or self.outcome is None:
                raise ValueError("completed history turns require a result")
            if self.error is not None:
                raise ValueError("completed history turns cannot expose an error")
            if self.outcome != self.assistant_result.outcome:
                raise ValueError("history outcome and result outcome must match")
            if self.action_cards != self.assistant_result.action_cards:
                raise ValueError("history action cards and result action cards differ")
        elif self.assistant_result is not None or self.outcome is not None:
            raise ValueError("only completed history turns expose results")

        if self.status in {TurnStatus.FAILED, TurnStatus.INTERRUPTED}:
            if self.error is None:
                raise ValueError("failed history turns require a safe error")
        elif self.error is not None:
            raise ValueError("this history state cannot expose an error")

        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal history turns require completed_at")
        if self.status not in terminal and self.completed_at is not None:
            raise ValueError("non-terminal history turns cannot have completed_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("history completion cannot precede creation")
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


class TurnSSEEnvelope(V2Contract):
    """Correlation and ordering fields shared by every v2 SSE data event."""

    sequence: int = Field(strict=True, gt=0)
    request_id: StableId
    trace_id: StableId
    turn_id: StableId


class TurnSSEProgressEvent(TurnSSEEnvelope):
    event: Literal[TurnSSEEventKind.PROGRESS] = TurnSSEEventKind.PROGRESS
    phase: TurnSSEProgressPhase
    turn_status: TurnStatus
    step_id: StableId | None = None
    capability: str | None = Field(default=None, pattern=ACTION_PATTERN)
    plan_revision: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_PLAN_REVISIONS,
    )
    step_status: TaskStatus | None = None
    reused: bool = False

    @model_validator(mode="after")
    def validate_progress(self) -> TurnSSEProgressEvent:
        step_phase = self.phase in {
            TurnSSEProgressPhase.STEP_STARTED,
            TurnSSEProgressPhase.STEP_FINISHED,
        }
        if step_phase != (self.step_id is not None):
            raise ValueError("step progress requires exactly one step identity")
        if step_phase != (self.capability is not None):
            raise ValueError("step progress requires exactly one capability")
        if step_phase != (self.plan_revision is not None):
            raise ValueError("step progress requires exactly one plan revision")
        if self.phase is TurnSSEProgressPhase.STEP_FINISHED:
            if self.step_status not in {
                TaskStatus.SUCCESS,
                TaskStatus.PARTIAL_SUCCESS,
                TaskStatus.FAILED,
            }:
                raise ValueError("finished step progress requires a terminal status")
        elif self.step_status is not None:
            raise ValueError("only finished step progress exposes a step status")
        if step_phase and self.turn_status is not TurnStatus.RUNNING:
            raise ValueError("step progress requires a running turn")
        if self.phase is TurnSSEProgressPhase.ADMITTED:
            if self.turn_status is not TurnStatus.PENDING:
                raise ValueError("admitted progress requires a pending turn")
        elif self.phase is TurnSSEProgressPhase.ATTACHED:
            if self.turn_status not in {TurnStatus.PENDING, TurnStatus.RUNNING}:
                raise ValueError("attached progress requires a live turn")
        elif self.phase is TurnSSEProgressPhase.CLAIMED:
            if self.turn_status is not TurnStatus.RUNNING:
                raise ValueError("claimed progress requires a running turn")
        if self.phase is not TurnSSEProgressPhase.STEP_FINISHED and self.reused:
            raise ValueError("only finished step progress can be reused")
        return self


class TurnSSETextDeltaEvent(TurnSSEEnvelope):
    event: Literal[TurnSSEEventKind.TEXT_DELTA] = TurnSSEEventKind.TEXT_DELTA
    turn_status: Literal[TurnStatus.COMPLETED] = TurnStatus.COMPLETED
    delta: str = Field(min_length=1, max_length=MAX_TEXT_DELTA_LENGTH)
    post_grounding: Literal[True] = True


class TurnCompletedTerminal(V2Contract):
    status: Literal[TurnStatus.COMPLETED]
    result: TurnResult
    usage: UsageSummary = Field(default_factory=UsageSummary)


class TurnFailedTerminal(V2Contract):
    status: Literal[TurnStatus.FAILED]
    error: SafeExecutionError
    usage: UsageSummary = Field(default_factory=UsageSummary)


class TurnCancelledTerminal(V2Contract):
    status: Literal[TurnStatus.CANCELLED]
    usage: UsageSummary = Field(default_factory=UsageSummary)


class TurnInterruptedTerminal(V2Contract):
    status: Literal[TurnStatus.INTERRUPTED]
    error: SafeExecutionError
    usage: UsageSummary = Field(default_factory=UsageSummary)


TurnTerminalPayload = Annotated[
    TurnCompletedTerminal
    | TurnFailedTerminal
    | TurnCancelledTerminal
    | TurnInterruptedTerminal,
    Field(discriminator="status"),
]


class TurnSSETerminalEvent(TurnSSEEnvelope):
    event: Literal[TurnSSEEventKind.TERMINAL] = TurnSSEEventKind.TERMINAL
    payload: TurnTerminalPayload
    reused_result: bool = False


TurnSSEEvent = Annotated[
    TurnSSEProgressEvent | TurnSSETextDeltaEvent | TurnSSETerminalEvent,
    Field(discriminator="event"),
]


class TurnSSESequence(V2Contract):
    """Validation helper for one completed SSE event sequence."""

    events: tuple[TurnSSEEvent, ...] = Field(
        min_length=1,
        max_length=MAX_SSE_EVENTS,
    )

    @model_validator(mode="after")
    def validate_stream(self) -> TurnSSESequence:
        sequences = tuple(event.sequence for event in self.events)
        if any(
            current <= previous
            for previous, current in zip(sequences, sequences[1:], strict=False)
        ):
            raise ValueError("SSE event sequence must be strictly increasing")
        correlation = (
            self.events[0].request_id,
            self.events[0].trace_id,
            self.events[0].turn_id,
        )
        if any(
            (event.request_id, event.trace_id, event.turn_id) != correlation
            for event in self.events[1:]
        ):
            raise ValueError("SSE events must share request, trace, and turn IDs")
        terminal_indexes = [
            index
            for index, event in enumerate(self.events)
            if isinstance(event, TurnSSETerminalEvent)
        ]
        if terminal_indexes != [len(self.events) - 1]:
            raise ValueError(
                "a completed SSE sequence requires one final terminal event"
            )
        return self


class ConversationDetailResponse(V2Contract):
    conversation: ConversationSummary
    turns: tuple[HistoryTurn, ...]


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


class ActionExecutionResponse(V2Contract):
    """Safe terminal result for a sandbox action execution attempt."""

    action_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: Literal[
        ActionStatus.EXECUTED,
        ActionStatus.EXPIRED,
        ActionStatus.CONFLICTED,
        ActionStatus.FAILED,
    ]
    resource_id: str = Field(pattern=IDENTIFIER_PATTERN)
    resource_version: ResourceVersion
    reused_result: bool = False


ActionResult = Annotated[
    ActionExecutionResponse | ActionDecisionResponse,
    Field(union_mode="left_to_right"),
]


class ActionReadResponse(V2Contract):
    """Public action state with a validated result and no storage metadata."""

    action: ActionCard
    result: ActionResult | None = None

    @classmethod
    def from_persistence(
        cls,
        *,
        action: ActionCard,
        persisted_result: Mapping[str, object] | None,
    ) -> ActionReadResponse:
        if persisted_result is None:
            return cls(action=action)
        if persisted_result == {"code": "conversation_deleted"}:
            return cls(action=action)
        envelope = _StoredActionResultEnvelope.model_validate(persisted_result)
        result = _ACTION_RESULT_ADAPTER.validate_python(envelope.response)
        return cls(action=action, result=result)

    @model_validator(mode="after")
    def validate_result_state(self) -> ActionReadResponse:
        pending = {ActionStatus.PROPOSED, ActionStatus.CONFIRMED}
        if self.action.status in pending:
            if self.result is not None:
                raise ValueError("pending actions cannot expose a terminal result")
            return self
        if self.result is None:
            return self
        if isinstance(
            self.result, ActionDecisionResponse
        ) and self.result.status not in {
            ActionStatus.REJECTED,
            ActionStatus.EXPIRED,
        }:
            raise ValueError("action decision result has an invalid status")
        if self.result.action_id != self.action.action_id:
            raise ValueError("action result ID must match the action card")
        if self.result.status != self.action.status:
            raise ValueError("action result status must match the action card")
        return self


class _StoredActionResultEnvelope(V2Contract):
    """Internal allowlist for the versioned persistence wrapper."""

    schema_version: Literal[1]
    response: dict[str, JsonValue]
    reason: str | None = Field(default=None, min_length=1, max_length=300)


_ACTION_RESULT_ADAPTER: TypeAdapter[ActionResult] = TypeAdapter(ActionResult)


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
