"""Strict internal contracts for bounded v2 read-turn execution.

These models carry validated operations and server-authored evidence between the
planner, read tools, supervisor, and grounding layer.  They deliberately exclude
model prompts, hidden reasoning, and unvalidated prose from durable traces.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from app.contracts import TaskStatus
from app.v2.contracts import (
    ACTION_PATTERN,
    IDENTIFIER_PATTERN,
    MAX_ADDED_READ_STEPS,
    MAX_CANDIDATES,
    MAX_DRAFT_REPAIRS,
    MAX_EXPERT_STEPS,
    MAX_KNOWLEDGE_RETRIEVALS,
    MAX_PLAN_REVISIONS,
    Citation,
    Claim,
    DialogueOutcome,
    EvidenceReference,
    ProductId,
    SafeExecutionError,
    StableId,
    V2Contract,
)
from app.v2.registry import ServiceId

_OPERATION_KEY_PATTERN = r"^[a-f0-9]{64}$"
_MAX_FACTS = 128
_MAX_EXCERPTS = 64  # Eight bounded steps may each contribute up to eight spans.
_MAX_REFERENCES = 80
_MAX_DRAFT_CLAIMS = 20

StrictDecimal = Annotated[Decimal, Field(strict=True, allow_inf_nan=False)]
FactValue = Annotated[
    StrictStr | StrictInt | StrictDecimal | StrictBool,
    Field(union_mode="left_to_right"),
]


class EvidenceObligationKind(StrEnum):
    """Allowlisted evidence requirements extracted and enforced by Python."""

    CATALOG = "catalog"
    COMPARISON = "comparison"
    PRICE = "price"
    REVIEW = "review"
    TRUST = "trust"
    KNOWLEDGE = "knowledge"
    MARKET = "market"
    INVENTORY = "inventory"


class EvidenceObligation(V2Contract):
    """One user or policy requirement that the final evidence must satisfy."""

    obligation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: EvidenceObligationKind
    description: str = Field(min_length=1, max_length=300)
    explicit: bool = False
    product_ids: tuple[ProductId, ...] = Field(default=(), max_length=MAX_CANDIDATES)

    @field_validator("product_ids")
    @classmethod
    def validate_product_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)):
            raise ValueError("obligation product IDs must be unique")
        return value


class RuntimeOperation(V2Contract):
    """One fully validated, version-bound deterministic read operation."""

    step_id: str = Field(pattern=IDENTIFIER_PATTERN)
    capability: str = Field(pattern=ACTION_PATTERN)
    service: ServiceId
    parameters: dict[str, JsonValue]
    depends_on: tuple[StableId, ...] = ()
    data_version_ids: tuple[StableId, ...] = Field(min_length=1)
    obligation_ids: tuple[StableId, ...] = ()
    operation_key: str = Field(pattern=_OPERATION_KEY_PATTERN)

    @field_validator("depends_on", "data_version_ids", "obligation_ids")
    @classmethod
    def validate_unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("operation references must be unique")
        return value

    @model_validator(mode="after")
    def validate_operation_key(self) -> RuntimeOperation:
        expected = build_operation_key(
            self.capability,
            self.parameters,
            self.data_version_ids,
        )
        if self.operation_key != expected:
            raise ValueError("operation key does not match validated operation")
        return self


class StructuredFact(V2Contract):
    """A typed value authored by a deterministic tool, never by a model."""

    fact_id: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    field: str = Field(pattern=ACTION_PATTERN)
    value: FactValue
    unit: str | None = Field(default=None, min_length=1, max_length=40)
    data_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    evidence_ids: tuple[StableId, ...] = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def restore_exact_decimal(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) == {"decimal"}:
            raw = value["decimal"]
            if not isinstance(raw, str):
                raise ValueError("serialized decimal fact must contain text")
            try:
                return Decimal(raw)
            except InvalidOperation as exc:
                raise ValueError("serialized decimal fact is invalid") from exc
        return value

    @field_serializer("value", when_used="json")
    def serialize_exact_decimal(self, value: FactValue) -> object:
        if isinstance(value, Decimal):
            return {"decimal": format(value, "f")}
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("fact evidence IDs must be unique")
        return value


class EvidenceExcerpt(V2Contract):
    """Exact checked text bound to one immutable source span."""

    evidence_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    chunk_id: str = Field(pattern=IDENTIFIER_PATTERN)
    span_id: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_ids: tuple[StableId, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    exact_text: str = Field(min_length=1, max_length=2_000)

    @field_validator("subject_ids")
    @classmethod
    def validate_subject_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("excerpt subject IDs must be unique")
        return value


class ToolEvidence(V2Contract):
    """Server-authored facts, exact spans, and their public source references."""

    facts: tuple[StructuredFact, ...] = Field(default=(), max_length=_MAX_FACTS)
    excerpts: tuple[EvidenceExcerpt, ...] = Field(default=(), max_length=_MAX_EXCERPTS)
    references: tuple[EvidenceReference, ...] = Field(
        default=(), max_length=_MAX_REFERENCES
    )

    @model_validator(mode="after")
    def validate_bindings(self) -> ToolEvidence:
        fact_ids = [fact.fact_id for fact in self.facts]
        excerpt_ids = [excerpt.evidence_id for excerpt in self.excerpts]
        reference_ids = [reference.evidence_id for reference in self.references]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("tool fact IDs must be unique")
        if len(excerpt_ids) != len(set(excerpt_ids)):
            raise ValueError("tool excerpt evidence IDs must be unique")
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("tool evidence reference IDs must be unique")

        references = {reference.evidence_id: reference for reference in self.references}
        for fact in self.facts:
            for evidence_id in fact.evidence_ids:
                reference = references.get(evidence_id)
                if reference is None:
                    raise ValueError("fact references unknown evidence")
                if reference.source_version_id != fact.data_version_id:
                    raise ValueError("fact and evidence data versions do not match")
        for excerpt in self.excerpts:
            reference = references.get(excerpt.evidence_id)
            if reference is None:
                raise ValueError("excerpt references unknown evidence")
            binding = (
                reference.source_id,
                reference.source_version_id,
                reference.chunk_id,
                reference.span_id,
            )
            expected = (
                excerpt.source_id,
                excerpt.source_version_id,
                excerpt.chunk_id,
                excerpt.span_id,
            )
            if binding != expected:
                raise ValueError("excerpt and evidence span bindings do not match")
        return self


class ExpertResult(V2Contract):
    """One terminal read result with deterministic output separate from reasoning."""

    operation: RuntimeOperation
    status: TaskStatus
    output: dict[str, JsonValue] | None = None
    evidence: ToolEvidence = Field(default_factory=ToolEvidence)
    selected_fact_ids: tuple[StableId, ...] = Field(default=(), max_length=8)
    selected_evidence_ids: tuple[StableId, ...] = Field(default=(), max_length=8)
    reasoning_fallback_reason: str | None = Field(
        default=None,
        pattern=ACTION_PATTERN,
        max_length=80,
    )
    error: SafeExecutionError | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_terminal_result(self) -> ExpertResult:
        if self.status not in {
            TaskStatus.SUCCESS,
            TaskStatus.PARTIAL_SUCCESS,
            TaskStatus.FAILED,
        }:
            raise ValueError("expert result must be terminal")
        if self.completed_at < self.started_at:
            raise ValueError("expert result completion cannot precede its start")
        if self.status == TaskStatus.SUCCESS:
            if self.output is None or self.error is not None:
                raise ValueError("successful expert results require output only")
        elif self.status == TaskStatus.FAILED:
            if self.output is not None or self.error is None:
                raise ValueError("failed expert results require a safe error only")
        elif self.output is None:
            raise ValueError("partial expert results require usable output")
        if len(self.selected_fact_ids) != len(set(self.selected_fact_ids)):
            raise ValueError("selected expert fact IDs must be unique")
        if len(self.selected_evidence_ids) != len(set(self.selected_evidence_ids)):
            raise ValueError("selected expert evidence IDs must be unique")
        fact_ids = {fact.fact_id for fact in self.evidence.facts}
        evidence_ids = {reference.evidence_id for reference in self.evidence.references}
        if not set(self.selected_fact_ids) <= fact_ids:
            raise ValueError("expert selected an unknown fact")
        if not set(self.selected_evidence_ids) <= evidence_ids:
            raise ValueError("expert selected unknown evidence")
        if self.status == TaskStatus.FAILED and (
            self.selected_fact_ids or self.selected_evidence_ids
        ):
            raise ValueError("failed expert results cannot select evidence")
        return self


class EvidenceAssessment(V2Contract):
    """Python-owned completeness, conflict, error, deadline, and budget state."""

    fulfilled_obligation_ids: tuple[StableId, ...] = ()
    missing_obligation_ids: tuple[StableId, ...] = ()
    conflicting_obligation_ids: tuple[StableId, ...] = ()
    candidate_product_ids: tuple[ProductId, ...] = Field(
        default=(), max_length=MAX_CANDIDATES
    )
    errors: tuple[SafeExecutionError, ...] = ()
    retryable_operation_keys: tuple[str, ...] = ()
    remaining_seconds: float = Field(ge=0, le=60)
    remaining_generation_calls: int = Field(strict=True, ge=0, le=10)
    remaining_provider_attempts: int = Field(strict=True, ge=0, le=16)
    remaining_cost_usd: Decimal = Field(ge=0, le=Decimal("0.25"))
    plan_revisions_used: int = Field(strict=True, ge=0, le=MAX_PLAN_REVISIONS)
    expert_steps_used: int = Field(strict=True, ge=0, le=MAX_EXPERT_STEPS)
    added_reads_used: int = Field(strict=True, ge=0, le=MAX_ADDED_READ_STEPS)
    knowledge_retrievals_used: int = Field(
        strict=True, ge=0, le=MAX_KNOWLEDGE_RETRIEVALS
    )
    draft_repairs_used: int = Field(strict=True, ge=0, le=MAX_DRAFT_REPAIRS)

    @field_validator(
        "fulfilled_obligation_ids",
        "missing_obligation_ids",
        "conflicting_obligation_ids",
        "candidate_product_ids",
        "retryable_operation_keys",
    )
    @classmethod
    def validate_unique_values(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(value) != len(set(value)):
            raise ValueError("assessment values must be unique")
        return value

    @model_validator(mode="after")
    def validate_obligation_partitions(self) -> EvidenceAssessment:
        fulfilled = set(self.fulfilled_obligation_ids)
        missing = set(self.missing_obligation_ids)
        conflicting = set(self.conflicting_obligation_ids)
        if fulfilled & missing or fulfilled & conflicting or missing & conflicting:
            raise ValueError("assessment obligation states must be disjoint")
        if len(self.retryable_operation_keys) > len(self.errors):
            raise ValueError("retryable operations require recorded safe errors")
        return self

    @property
    def can_continue(self) -> bool:
        return bool(
            self.missing_obligation_ids
            and not self.conflicting_obligation_ids
            and self.plan_revisions_used < MAX_PLAN_REVISIONS
            and self.added_reads_used < MAX_ADDED_READ_STEPS
            and self.expert_steps_used < MAX_EXPERT_STEPS
            and self.remaining_seconds > 0
        )


class DraftClaim(V2Contract):
    """An unchecked selector draft; only grounding may turn it into public text."""

    claim_id: str = Field(pattern=IDENTIFIER_PATTERN)
    text: str | None = Field(default=None, min_length=1, max_length=1_000)
    fact_ids: tuple[StableId, ...] = ()
    evidence_ids: tuple[StableId, ...] = ()

    @field_validator("fact_ids", "evidence_ids")
    @classmethod
    def validate_unique_selectors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("draft selectors must be unique")
        return value

    @model_validator(mode="after")
    def validate_claim_shape(self) -> DraftClaim:
        if self.fact_ids:
            if self.text is not None:
                raise ValueError("structured fact drafts cannot supply prose")
        elif self.text is None or not self.evidence_ids:
            raise ValueError("knowledge drafts require text and evidence")
        return self


class AnswerDraft(V2Contract):
    """A bounded set of claim selectors with no standalone answer prose."""

    claims: tuple[DraftClaim, ...] = Field(
        min_length=1,
        max_length=_MAX_DRAFT_CLAIMS,
    )

    @model_validator(mode="after")
    def validate_claim_ids(self) -> AnswerDraft:
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("draft claim IDs must be unique")
        return self


class GroundingResult(V2Contract):
    """The only answer-layer result the supervisor may publish."""

    outcome: DialogueOutcome
    answer: str = Field(min_length=1, max_length=20_000)
    claims: tuple[Claim, ...] = ()
    citations: tuple[Citation, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    warnings: tuple[str, ...] = ()
    draft_repairs: int = Field(default=0, strict=True, ge=0, le=MAX_DRAFT_REPAIRS)

    @model_validator(mode="after")
    def validate_public_references(self) -> GroundingResult:
        claim_ids = {claim.claim_id for claim in self.claims}
        citation_ids = {citation.citation_id for citation in self.citations}
        evidence_ids = {reference.evidence_id for reference in self.evidence}
        if len(claim_ids) != len(self.claims):
            raise ValueError("grounded claim IDs must be unique")
        if len(citation_ids) != len(self.citations):
            raise ValueError("grounded citation IDs must be unique")
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("grounded evidence IDs must be unique")
        citations = {citation.citation_id: citation for citation in self.citations}
        for claim in self.claims:
            if not set(claim.citation_ids) <= citation_ids:
                raise ValueError("grounded claim references unknown citation")
            if any(
                citations[citation_id].claim_id != claim.claim_id
                for citation_id in claim.citation_ids
            ):
                raise ValueError("grounded claim references another claim's citation")
        for citation in self.citations:
            if citation.claim_id not in claim_ids:
                raise ValueError("grounded citation references unknown claim")
            if citation.evidence_id not in evidence_ids:
                raise ValueError("grounded citation references unknown evidence")
        return self


def build_operation_key(
    capability: str,
    parameters: dict[str, JsonValue],
    data_version_ids: tuple[str, ...],
) -> str:
    """Bind one operation to its validated input and immutable data snapshots."""

    payload = {
        "capability": capability,
        "parameters": parameters,
        "data_version_ids": sorted(data_version_ids),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "AnswerDraft",
    "DraftClaim",
    "EvidenceAssessment",
    "EvidenceExcerpt",
    "EvidenceObligation",
    "EvidenceObligationKind",
    "ExpertResult",
    "GroundingResult",
    "RuntimeOperation",
    "StructuredFact",
    "ToolEvidence",
    "build_operation_key",
]
