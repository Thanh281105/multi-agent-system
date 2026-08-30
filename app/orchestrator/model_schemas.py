"""Strict Structured Output schemas for model-assisted orchestration stages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SupportedIntent = Literal[
    "general.help",
    "general.unsupported",
    "product.search",
    "product.rank",
    "product.compare",
    "product.follow_up",
    "review.summary",
    "trust.complaints",
    "market.analyze",
    "market.search",
    "multi.recommendation",
]

CapabilityName = Literal[
    "product.search",
    "product.rank",
    "product.compare",
    "review.summarize",
    "review.compare",
    "trust.complaints",
    "trust.compare",
    "market.analyze",
    "market.search",
]


class RoutingEntities(BaseModel):
    """Bounded model-extracted entities; unknown keys are impossible."""

    model_config = ConfigDict(extra="forbid")

    category: str | None = Field(default=None, max_length=80)
    max_price: int | None = Field(default=None, ge=0, le=1_000_000_000)
    min_price: int | None = Field(default=None, ge=0, le=1_000_000_000)
    min_rating: float | None = Field(default=None, ge=0, le=5)
    author: str | None = Field(default=None, max_length=220)
    publisher: str | None = Field(default=None, max_length=220)
    min_page_count: int | None = Field(default=None, ge=1, le=20_000)
    max_page_count: int | None = Field(default=None, ge=1, le=20_000)
    product_id: int | None = Field(default=None, ge=1)
    product_query: str | None = Field(default=None, max_length=220)
    product_queries: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_price_range(self) -> RoutingEntities:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price must not exceed max_price")
        if (
            self.min_page_count is not None
            and self.max_page_count is not None
            and self.min_page_count > self.max_page_count
        ):
            raise ValueError("min_page_count must not exceed max_page_count")
        return self


class RoutingDecision(BaseModel):
    """Semantic route proposal compiled into the provider-neutral contract."""

    model_config = ConfigDict(extra="forbid")

    intent: SupportedIntent
    confidence: float = Field(ge=0, le=1)
    entities: RoutingEntities
    rationale: str = Field(min_length=1, max_length=160)


class PlanningDecision(BaseModel):
    """Declarative capabilities; Python remains the authorized DAG compiler."""

    model_config = ConfigDict(extra="forbid")

    capabilities: list[CapabilityName] = Field(max_length=8)
    candidate_limit: int = Field(default=5, ge=1, le=5)
    rationale: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_unique_capabilities(self) -> PlanningDecision:
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must be unique")
        return self


class GroundedClaim(BaseModel):
    """Reference to one server-authored answer claim."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=r"^claim_[0-9]{3}$")


class GroundedSynthesis(BaseModel):
    """Ordering of server-authored, evidence-bound answer claims."""

    model_config = ConfigDict(extra="forbid")

    claims: list[GroundedClaim] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_unique_claims(self) -> GroundedSynthesis:
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("grounded claim IDs must be unique")
        return self
