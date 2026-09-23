"""Strict loading and evidence validation for the frozen Evaluation v3 gold corpus.

This module is additive.  It does not read v1/v2 result captures and it never
derives gold from system output.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_models import PACKAGE7_PILOT_CASE_ORDER
from app.v2.contracts import ConversationMode
from app.v2.registry import (
    CapabilityEffect,
    ConfirmationPolicy,
    build_default_v2_registry,
)

_IDENTIFIER = r"^[a-z][a-z0-9_.-]{2,127}$"
_SHA256 = r"^[a-f0-9]{64}$"

IdentifierV3 = Annotated[str, Field(pattern=_IDENTIFIER)]
Sha256V3 = Annotated[str, Field(pattern=_SHA256)]
ScalarV3: TypeAlias = str | int | float | bool | None

PACKAGE7_CATEGORY_ORDER = (
    "multi_constraint",
    "knowledge_source",
    "multi_expert_compare_recommendation",
    "multi_turn_memory",
    "insufficient_conflict_injection",
    "shopping_merchant",
)
PACKAGE7_DEVELOPMENT_QUOTAS = (4, 4, 4, 3, 3, 2)
PACKAGE7_HELDOUT_QUOTAS = (12, 12, 12, 8, 8, 8)
PACKAGE7_PRIOR_EXPOSED_PRODUCT_IDS = (
    6,
    7,
    29,
    43,
    46,
    59,
    60,
    61,
    80,
    91,
    94,
    132,
    137,
    155,
    165,
    187,
    196,
)
PACKAGE7_DEVELOPMENT_PRODUCT_IDS = (80, 168, 179, 101)
PACKAGE7_DEVELOPMENT_WORK_IDS = (
    "work_sapiens_yuval_noah_harari",
    "work_de_men_phieu_luu_ky_to_hoai",
    "work_zero_to_one_peter_thiel_blake_masters",
    "work_flour_water_salt_yeast_ken_forkish",
)
PACKAGE7_DEVELOPMENT_SOURCE_IDS = (
    "src_sapiens_author",
    "src_de_men_kim_dong",
    "src_zero_to_one_prh",
    "src_flour_water_salt_yeast_prh",
)
PACKAGE7_DEVELOPMENT_NEGATIVE_FAMILIES = (
    "network_router_vlan_configuration",
    "motorcycle_oil_maintenance",
    "origami_dragon_folding",
    "clothing_zipper_repair",
)
PACKAGE7_REQUIRED_FACT_COUNT = 264
_BLOCKED_PROVENANCE_FRAGMENTS = (
    "evaluation/results",
    "evaluation\\results",
    "sut_output",
    "sut-output",
    "captured_output",
    "observations.json",
)
_BLOCKED_PROVENANCE_KEYS = {
    "sut_output",
    "captured_output",
    "model_output",
    "observation",
    "result_artifact",
}
_OUTCOME_LEAKAGE_FRAGMENTS = (
    "chờ tôi xác nhận",
    "dừng chờ",
    "không thực hiện giao dịch",
    "không áp dụng ưu đãi",
    "không có quyền",
    "từ chối",
    "không tạo đề xuất",
    "chưa có đề xuất",
    "wait for confirmation",
    "missing permission",
    "must deny",
    "no confirmed proposal",
    "refuse",
)
_INTERNAL_PROMPT_FRAGMENTS = (
    "trong sandbox",
    "identity_fixture_id",
    "target_capability_id",
    "expected_version",
    "confirmed_proposal_id",
    "proposal_parameters",
    "work_group_id",
    "prompt_family_id",
)
_SPLIT_ALIAS_TOKENS = frozenset({"dev", "development", "held", "heldout", "test"})
_PROMPT_FILLER_PATTERNS = (
    r"\bxin\s+vui\s+lòng\b",
    r"\bvui\s+lòng\b",
    r"\blàm\s+ơn\b",
    r"\bgiúp\s+(?:tôi|mình)\b",
    r"\btôi\s+muốn\b",
    r"\bhãy\b",
)
_SEMANTIC_TEMPLATE_SIMILARITY_THRESHOLD = 0.60
_CATALOG_FIELDS_BY_CAPABILITY = {
    "product.catalog.search": frozenset({"name", "price_vnd", "rating"}),
    "product.compare": frozenset({"name", "price_vnd", "rating"}),
    "product.rank": frozenset({"name", "price_vnd", "rating"}),
    "merchant.catalog.read": frozenset({"name", "price_vnd", "rating"}),
    "shopper.cart.read": frozenset({"name", "price_vnd"}),
    "shopper.checkout.preview": frozenset({"name", "price_vnd"}),
    "merchant.inventory.read": frozenset({"price_vnd"}),
}


class FrozenGoldContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EvaluationSplitV3(StrEnum):
    DEVELOPMENT = "development"
    HELD_OUT = "held_out"


class EvaluationCategoryV3(StrEnum):
    MULTI_CONSTRAINT = "multi_constraint"
    KNOWLEDGE_SOURCE = "knowledge_source"
    MULTI_EXPERT_COMPARE_RECOMMENDATION = "multi_expert_compare_recommendation"
    MULTI_TURN_MEMORY = "multi_turn_memory"
    INSUFFICIENT_CONFLICT_INJECTION = "insufficient_conflict_injection"
    SHOPPING_MERCHANT = "shopping_merchant"


class AnswerabilityV3(StrEnum):
    ANSWERABLE = "answerable"
    PARTIALLY_ANSWERABLE = "partially_answerable"
    UNANSWERABLE = "unanswerable"
    CONFLICTED_REQUIRES_CLARIFICATION = "conflicted_requires_clarification"


class RequiredResponseModeV3(StrEnum):
    DIRECT_ANSWER = "direct_answer"
    ANSWER_WITH_LIMITATION = "answer_with_limitation"
    ABSTAIN = "abstain"
    ASK_CLARIFICATION = "ask_clarification"
    AWAIT_CONFIRMATION = "await_confirmation"
    DENY = "deny"


class IdentityRoleV3(StrEnum):
    SHOPPER = "shopper"
    MERCHANT = "merchant"


class IdentityScopeV3(StrEnum):
    ECOMMERCE_READ = "ecommerce.read"
    ECOMMERCE_WRITE = "ecommerce.write"
    MERCHANT_READ = "merchant.read"
    MERCHANT_WRITE = "merchant.write"


class ActionBoundaryOutcomeV3(StrEnum):
    COMPLETE = "complete"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DENIED = "denied"


class ConfirmationStateV3(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"


class LeakageKeyKindV3(StrEnum):
    PROMPT_FAMILY = "prompt_family"
    SCENARIO_FAMILY = "scenario_family"
    PARAPHRASE_FAMILY = "paraphrase_family"
    PRODUCT = "product"
    WORK = "work"
    SOURCE = "source"
    SPLIT_NAMESPACE = "split_namespace"


class CategoryQuotaV3(FrozenGoldContractV3):
    multi_constraint: int = Field(ge=0)
    knowledge_source: int = Field(ge=0)
    multi_expert_compare_recommendation: int = Field(ge=0)
    multi_turn_memory: int = Field(ge=0)
    insufficient_conflict_injection: int = Field(ge=0)
    shopping_merchant: int = Field(ge=0)
    total: int = Field(ge=0)

    def ordered_counts(self) -> tuple[int, ...]:
        return (
            self.multi_constraint,
            self.knowledge_source,
            self.multi_expert_compare_recommendation,
            self.multi_turn_memory,
            self.insufficient_conflict_injection,
            self.shopping_merchant,
        )


class QuotaPlanV3(FrozenGoldContractV3):
    development: CategoryQuotaV3
    held_out: CategoryQuotaV3


class SourceAssetV3(FrozenGoldContractV3):
    path: str = Field(min_length=1, max_length=500)
    sha256: Sha256V3
    records: int | None = Field(default=None, ge=1)
    used_for_required_facts: bool | None = None


class SourceAssetsV3(FrozenGoldContractV3):
    products: SourceAssetV3
    reviews: SourceAssetV3
    sources: SourceAssetV3
    mappings: SourceAssetV3
    snapshot_manifest: SourceAssetV3
    development_probes: SourceAssetV3


class UserTurnV3(FrozenGoldContractV3):
    turn_id: IdentifierV3
    ordinal: int = Field(ge=1, le=4)
    message: str = Field(min_length=2, max_length=2_000)


class IdentityFixtureV3(FrozenGoldContractV3):
    identity_fixture_id: IdentifierV3
    role: IdentityRoleV3
    authenticated: Literal[True] = True
    scopes: tuple[IdentityScopeV3, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_role_scopes(self) -> IdentityFixtureV3:
        allowed = (
            {
                (IdentityScopeV3.ECOMMERCE_READ,),
                (
                    IdentityScopeV3.ECOMMERCE_READ,
                    IdentityScopeV3.ECOMMERCE_WRITE,
                ),
            }
            if self.role == IdentityRoleV3.SHOPPER
            else {
                (
                    IdentityScopeV3.ECOMMERCE_READ,
                    IdentityScopeV3.MERCHANT_READ,
                ),
                (
                    IdentityScopeV3.ECOMMERCE_READ,
                    IdentityScopeV3.MERCHANT_READ,
                    IdentityScopeV3.MERCHANT_WRITE,
                ),
            }
        )
        if self.scopes not in allowed:
            raise ValueError("identity role must use a frozen exact scope combination")
        expected_fixture = {
            (IdentityRoleV3.SHOPPER, (IdentityScopeV3.ECOMMERCE_READ,)): (
                "identity_shopper_readonly_v3"
            ),
            (
                IdentityRoleV3.SHOPPER,
                (
                    IdentityScopeV3.ECOMMERCE_READ,
                    IdentityScopeV3.ECOMMERCE_WRITE,
                ),
            ): "identity_shopper_write_v3",
            (
                IdentityRoleV3.MERCHANT,
                (
                    IdentityScopeV3.ECOMMERCE_READ,
                    IdentityScopeV3.MERCHANT_READ,
                ),
            ): "identity_merchant_readonly_v3",
            (
                IdentityRoleV3.MERCHANT,
                (
                    IdentityScopeV3.ECOMMERCE_READ,
                    IdentityScopeV3.MERCHANT_READ,
                    IdentityScopeV3.MERCHANT_WRITE,
                ),
            ): "identity_merchant_write_v3",
        }[(self.role, self.scopes)]
        if self.identity_fixture_id != expected_fixture:
            raise ValueError("identity fixture ID and exact role scopes disagree")
        return self


class CapabilityContractV3(FrozenGoldContractV3):
    capability_id: IdentifierV3
    allowed_modes: tuple[IdentityRoleV3, ...] = Field(min_length=1, max_length=2)
    required_scopes: tuple[IdentityScopeV3, ...] = Field(min_length=1, max_length=3)
    effect: CapabilityEffect
    confirmation_policy: ConfirmationPolicy


class ActionCapabilityBlueprintV3(FrozenGoldContractV3):
    allowed: tuple[CapabilityContractV3, ...]
    required: tuple[IdentifierV3, ...]
    forbidden: tuple[IdentifierV3, ...]
    attempted: tuple[CapabilityContractV3, ...]
    expected_outcome: ActionBoundaryOutcomeV3
    confirmation_state: ConfirmationStateV3

    @model_validator(mode="after")
    def validate_partition(self) -> ActionCapabilityBlueprintV3:
        allowed_ids = tuple(item.capability_id for item in self.allowed)
        allowed = set(allowed_ids)
        required = set(self.required)
        forbidden = set(self.forbidden)
        if len(allowed) != len(allowed_ids) or len(required) != len(self.required):
            raise ValueError("action capability lists must be unique")
        if len(forbidden) != len(self.forbidden):
            raise ValueError("forbidden action capabilities must be unique")
        if not required.issubset(allowed):
            raise ValueError("required actions must be allowed")
        if allowed & forbidden:
            raise ValueError("allowed and forbidden actions cannot overlap")
        attempted_ids = {item.capability_id for item in self.attempted}
        if self.expected_outcome == ActionBoundaryOutcomeV3.DENIED:
            if required or not self.attempted:
                raise ValueError("denied boundaries cannot require actions")
            if any(item.effect != CapabilityEffect.READ for item in self.allowed):
                raise ValueError("denied boundaries may retain read capabilities only")
            if not attempted_ids.issubset(forbidden):
                raise ValueError("denied attempts must remain forbidden")
        elif attempted_ids != required:
            raise ValueError("successful attempts must equal required capabilities")
        if len({item.capability_id for item in self.attempted}) != len(self.attempted):
            raise ValueError("attempted capabilities must be unique")
        proposal_required = any(
            item.effect == CapabilityEffect.PROPOSAL and item.capability_id in required
            for item in self.allowed
        )
        execute_required = any(
            item.effect == CapabilityEffect.EXECUTE and item.capability_id in required
            for item in self.allowed
        )
        if (
            execute_required
            and self.confirmation_state != ConfirmationStateV3.CONFIRMED
        ):
            raise ValueError(
                "execute cannot be required without confirmed proposal state"
            )
        if proposal_required:
            if (
                self.expected_outcome != ActionBoundaryOutcomeV3.AWAITING_CONFIRMATION
                or self.confirmation_state != ConfirmationStateV3.UNCONFIRMED
                or execute_required
            ):
                raise ValueError("proposal boundaries must stop awaiting confirmation")
        if (
            self.expected_outcome == ActionBoundaryOutcomeV3.COMPLETE
            and self.confirmation_state != ConfirmationStateV3.NOT_APPLICABLE
        ):
            raise ValueError("completed read boundaries have no confirmation state")
        return self


class SourceExcerptSupportV3(FrozenGoldContractV3):
    kind: Literal["source_excerpt"]
    artifact_path: Literal["data/knowledge/books-v1/sources.json"]
    artifact_sha256: Sha256V3
    json_pointer: str = Field(pattern=r"^/sources/[0-9]+/content_markdown$")
    record_id: IdentifierV3
    record_content_sha256: Sha256V3
    span_unit: Literal["unicode_codepoint"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    exact_excerpt: str = Field(min_length=1, max_length=2_000)
    support_scope: Literal["work"]

    @model_validator(mode="after")
    def validate_span(self) -> SourceExcerptSupportV3:
        if self.end <= self.start:
            raise ValueError("source evidence span must be non-empty")
        if self.end - self.start != len(self.exact_excerpt):
            raise ValueError("source evidence span length must match its excerpt")
        return self


class CatalogPointerSupportV3(FrozenGoldContractV3):
    kind: Literal["catalog_pointer"]
    artifact_path: Literal["data/snapshots/tiki-books-v4-eval/products.jsonl"]
    artifact_sha256: Sha256V3
    record_line_1based: int = Field(ge=1, le=200)
    record_id: str = Field(pattern=r"^product_[1-9][0-9]{0,2}$")
    json_pointer: str = Field(pattern=r"^/[a-z][a-z0-9_]*$")
    expected_value: ScalarV3
    support_scope: Literal["catalog_record"]

    @model_validator(mode="after")
    def validate_record_identity(self) -> CatalogPointerSupportV3:
        if self.record_id != f"product_{self.record_line_1based}":
            raise ValueError("catalog record ID must match its one-based line")
        return self


EvidenceSupportV3 = Annotated[
    SourceExcerptSupportV3 | CatalogPointerSupportV3,
    Field(discriminator="kind"),
]


class RequiredFactBlueprintV3(FrozenGoldContractV3):
    fact_id: IdentifierV3
    claim_blueprint: str = Field(min_length=1, max_length=2_000)
    expected_value: ScalarV3
    match_mode: Literal[
        "fact_semantics",
        "normalized_exact",
        "numeric_exact",
    ]
    support: EvidenceSupportV3


class ForbiddenFactBlueprintV3(FrozenGoldContractV3):
    fact_id: IdentifierV3
    matcher_blueprint: str = Field(min_length=1, max_length=2_000)
    reason: IdentifierV3


class MemoryWriteV3(FrozenGoldContractV3):
    slot: IdentifierV3
    value: str | int | float | bool


class MemoryEffectV3(FrozenGoldContractV3):
    turn_id: IdentifierV3
    writes: tuple[MemoryWriteV3, ...]
    reads: tuple[IdentifierV3, ...]


class LeakageKeyV3(FrozenGoldContractV3):
    kind: LeakageKeyKindV3
    value: str = Field(min_length=1, max_length=300)


class SandboxCartLineV3(FrozenGoldContractV3):
    product_id: int = Field(ge=1, le=200)
    quantity: int = Field(ge=1, le=20)
    unit_price_vnd: int = Field(ge=0, le=10_000_000_000)


class SandboxCartStateV3(FrozenGoldContractV3):
    cart_id: IdentifierV3
    version: Literal[1]
    lines: tuple[SandboxCartLineV3, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_products(self) -> SandboxCartStateV3:
        product_ids = tuple(line.product_id for line in self.lines)
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("sandbox cart product IDs must be unique")
        return self


class SandboxOfferV3(FrozenGoldContractV3):
    offer_id: IdentifierV3
    product_id: int = Field(ge=1, le=200)
    price_vnd: int = Field(ge=0, le=10_000_000_000)
    available_quantity: int = Field(ge=0, le=1_000_000)
    version: Literal[1]


class SandboxMerchantStateV3(FrozenGoldContractV3):
    snapshot_version_id: IdentifierV3
    offers: tuple[SandboxOfferV3, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_products_and_offers(self) -> SandboxMerchantStateV3:
        product_ids = tuple(offer.product_id for offer in self.offers)
        offer_ids = tuple(offer.offer_id for offer in self.offers)
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("sandbox merchant product IDs must be unique")
        if len(offer_ids) != len(set(offer_ids)):
            raise ValueError("sandbox merchant offer IDs must be unique")
        return self


class SandboxCheckoutProposalV3(FrozenGoldContractV3):
    kind: Literal["checkout_proposal"]
    capability_id: Literal["shopper.checkout.propose"]
    cart_id: IdentifierV3
    expected_version: Literal[1]


class SandboxOfferProposalV3(FrozenGoldContractV3):
    kind: Literal["offer_proposal"]
    capability_id: Literal["merchant.offer.propose"]
    offer_id: IdentifierV3
    expected_version: Literal[1]
    new_price_vnd: int = Field(ge=0, le=10_000_000_000)


SandboxProposalV3 = Annotated[
    SandboxCheckoutProposalV3 | SandboxOfferProposalV3,
    Field(discriminator="kind"),
]


class SandboxFixtureV3(FrozenGoldContractV3):
    fixture_id: IdentifierV3
    reset_revision: Literal[1]
    cart: SandboxCartStateV3 | None
    merchant: SandboxMerchantStateV3 | None
    target_capability_id: Literal[
        "shopper.checkout.propose",
        "merchant.offer.propose",
        "shopper.checkout.execute",
        "merchant.offer.execute",
    ]
    proposal_parameters: SandboxProposalV3 | None
    confirmed_proposal_id: Literal[None]

    @model_validator(mode="after")
    def validate_one_state_domain(self) -> SandboxFixtureV3:
        if (self.cart is None) == (self.merchant is None):
            raise ValueError("sandbox fixture must freeze exactly one state domain")
        if self.target_capability_id.endswith(".propose"):
            if (
                self.proposal_parameters is None
                or self.proposal_parameters.capability_id != self.target_capability_id
            ):
                raise ValueError("proposal target requires exact frozen parameters")
        elif self.proposal_parameters is not None:
            raise ValueError("execute denial starts without proposal parameters")
        if isinstance(self.proposal_parameters, SandboxCheckoutProposalV3):
            if self.cart is None:
                raise ValueError("checkout proposal requires frozen cart state")
            if (
                self.proposal_parameters.cart_id != self.cart.cart_id
                or self.proposal_parameters.expected_version != self.cart.version
            ):
                raise ValueError("checkout proposal parameters differ from cart state")
        if isinstance(self.proposal_parameters, SandboxOfferProposalV3):
            if self.merchant is None:
                raise ValueError("offer proposal requires frozen merchant state")
            offers = {offer.offer_id: offer for offer in self.merchant.offers}
            offer = offers.get(self.proposal_parameters.offer_id)
            if (
                offer is None
                or self.proposal_parameters.expected_version != offer.version
            ):
                raise ValueError("offer proposal parameters differ from offer state")
        return self


class GoldConversationV3(FrozenGoldContractV3):
    conversation_id: IdentifierV3
    split: EvaluationSplitV3
    category: EvaluationCategoryV3
    work_group_id: IdentifierV3
    user_turns: tuple[UserTurnV3, ...] = Field(min_length=1, max_length=4)
    identity_fixture: IdentityFixtureV3
    product_ids: tuple[int, ...]
    work_ids: tuple[IdentifierV3, ...]
    source_ids: tuple[IdentifierV3, ...]
    prompt_family_id: IdentifierV3
    scenario_family_id: IdentifierV3
    paraphrase_family_id: IdentifierV3 | None = None
    answerability: AnswerabilityV3
    required_response_mode: RequiredResponseModeV3
    required_fact_blueprints: tuple[RequiredFactBlueprintV3, ...]
    forbidden_fact_blueprints: tuple[ForbiddenFactBlueprintV3, ...]
    action_capability_blueprint: ActionCapabilityBlueprintV3
    sandbox_fixture: SandboxFixtureV3 | None
    memory_effects: tuple[MemoryEffectV3, ...]
    leakage_keys: tuple[LeakageKeyV3, ...] = Field(min_length=3)
    gold_origin: Literal["automated_pre_sut_spec"]

    @model_validator(mode="after")
    def validate_conversation(self) -> GoldConversationV3:
        expected_ordinals = tuple(range(1, len(self.user_turns) + 1))
        if tuple(turn.ordinal for turn in self.user_turns) != expected_ordinals:
            raise ValueError("user turn ordinals must be contiguous from one")
        if any(
            turn.turn_id != f"{self.conversation_id}_t{turn.ordinal}"
            for turn in self.user_turns
        ):
            raise ValueError(
                "turn IDs must be derived from conversation ID and ordinal"
            )
        if self.category == EvaluationCategoryV3.MULTI_TURN_MEMORY:
            if len(self.user_turns) < 2 or not self.memory_effects:
                raise ValueError("multi-turn memory cases require turns and effects")
        elif len(self.user_turns) != 1 or self.memory_effects:
            raise ValueError("only multi-turn memory cases may declare memory effects")
        turn_ids = {turn.turn_id for turn in self.user_turns}
        if any(effect.turn_id not in turn_ids for effect in self.memory_effects):
            raise ValueError("memory effects must reference a conversation turn")
        if len({fact.fact_id for fact in self.required_fact_blueprints}) != len(
            self.required_fact_blueprints
        ):
            raise ValueError("required fact IDs must be unique within a conversation")
        if len({fact.fact_id for fact in self.forbidden_fact_blueprints}) != len(
            self.forbidden_fact_blueprints
        ):
            raise ValueError("forbidden fact IDs must be unique within a conversation")
        if (
            self.action_capability_blueprint.expected_outcome
            == ActionBoundaryOutcomeV3.AWAITING_CONFIRMATION
        ):
            expected_mode = RequiredResponseModeV3.AWAIT_CONFIRMATION
        elif (
            self.action_capability_blueprint.expected_outcome
            == ActionBoundaryOutcomeV3.DENIED
        ):
            expected_mode = RequiredResponseModeV3.DENY
        else:
            expected_mode = {
                AnswerabilityV3.ANSWERABLE: RequiredResponseModeV3.DIRECT_ANSWER,
                AnswerabilityV3.PARTIALLY_ANSWERABLE: (
                    RequiredResponseModeV3.ANSWER_WITH_LIMITATION
                ),
                AnswerabilityV3.UNANSWERABLE: RequiredResponseModeV3.ABSTAIN,
                AnswerabilityV3.CONFLICTED_REQUIRES_CLARIFICATION: (
                    RequiredResponseModeV3.ASK_CLARIFICATION
                ),
            }[self.answerability]
        if self.required_response_mode != expected_mode:
            raise ValueError("answerability and required response mode disagree")
        if (
            self.action_capability_blueprint.expected_outcome
            == ActionBoundaryOutcomeV3.DENIED
        ):
            if self.answerability != AnswerabilityV3.UNANSWERABLE:
                raise ValueError("denied actions must be marked unanswerable")
            if self.required_fact_blueprints:
                raise ValueError("denied actions cannot require informational facts")
        elif self.answerability in {
            AnswerabilityV3.ANSWERABLE,
            AnswerabilityV3.PARTIALLY_ANSWERABLE,
        }:
            if not self.required_fact_blueprints:
                raise ValueError("answerable conversations require supported facts")
        elif self.required_fact_blueprints:
            raise ValueError("non-answerable conversations cannot require facts")
        keys = {(key.kind, key.value) for key in self.leakage_keys}
        if len(keys) != len(self.leakage_keys):
            raise ValueError("leakage keys must be unique within a conversation")
        required_keys = {
            (LeakageKeyKindV3.PROMPT_FAMILY, self.prompt_family_id),
            (LeakageKeyKindV3.SCENARIO_FAMILY, self.scenario_family_id),
            (LeakageKeyKindV3.SPLIT_NAMESPACE, self.split.value),
            *((LeakageKeyKindV3.PRODUCT, str(value)) for value in self.product_ids),
            *((LeakageKeyKindV3.WORK, value) for value in self.work_ids),
            *((LeakageKeyKindV3.SOURCE, value) for value in self.source_ids),
        }
        if self.paraphrase_family_id is not None:
            required_keys.add(
                (LeakageKeyKindV3.PARAPHRASE_FAMILY, self.paraphrase_family_id)
            )
        if not required_keys.issubset(keys):
            raise ValueError(
                "conversation resource and family leakage keys are incomplete"
            )
        if len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("conversation product IDs must be unique")
        if len(self.work_ids) != len(set(self.work_ids)):
            raise ValueError("conversation work IDs must be unique")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("conversation source IDs must be unique")
        if self.category == EvaluationCategoryV3.SHOPPING_MERCHANT:
            if self.sandbox_fixture is None:
                raise ValueError("shopping cases require an explicit sandbox fixture")
            if self.sandbox_fixture.fixture_id != f"sandbox_{self.conversation_id}":
                raise ValueError(
                    "sandbox fixture ID must be derived from conversation ID"
                )
            if self.sandbox_fixture.cart is not None:
                fixture_product_ids = tuple(
                    line.product_id for line in self.sandbox_fixture.cart.lines
                )
            else:
                merchant_state = self.sandbox_fixture.merchant
                if merchant_state is None:  # guarded by SandboxFixtureV3
                    raise ValueError("shopping sandbox state is absent")
                fixture_product_ids = tuple(
                    offer.product_id for offer in merchant_state.offers
                )
            if set(fixture_product_ids) != set(self.product_ids):
                raise ValueError("sandbox fixture products must exactly match the case")
            boundary_ids = {
                item.capability_id
                for item in (
                    *self.action_capability_blueprint.allowed,
                    *self.action_capability_blueprint.attempted,
                )
            }
            if self.sandbox_fixture.target_capability_id not in boundary_ids:
                raise ValueError(
                    "sandbox target capability is absent from the boundary"
                )
            uses_shopper_state = any(
                capability_id.startswith("shopper.") for capability_id in boundary_ids
            )
            if uses_shopper_state != (self.sandbox_fixture.cart is not None):
                raise ValueError("sandbox state domain must match action capabilities")
            if (
                self.action_capability_blueprint.confirmation_state
                != ConfirmationStateV3.UNCONFIRMED
            ):
                raise ValueError("shopping sandbox must start without confirmation")
        elif self.sandbox_fixture is not None:
            raise ValueError("only shopping cases may declare a sandbox fixture")
        return self


class EvaluationGoldV3(FrozenGoldContractV3):
    schema_version: Literal["3.0"]
    corpus_id: IdentifierV3
    gold_origin: Literal["automated_pre_sut_spec"]
    category_order: tuple[EvaluationCategoryV3, ...]
    quota_plan: QuotaPlanV3
    frozen_pilot_ids: tuple[IdentifierV3, ...]
    source_assets: SourceAssetsV3
    registry_contract_sha256: Sha256V3
    conversations: tuple[GoldConversationV3, ...] = Field(min_length=80, max_length=80)

    @model_validator(mode="after")
    def validate_frozen_corpus(self) -> EvaluationGoldV3:
        _validate_frozen_identity(
            self.category_order,
            self.quota_plan,
            self.frozen_pilot_ids,
        )
        _validate_conversation_roster(self.conversations)
        _validate_semantic_validity(self.conversations)
        if (
            sum(len(case.required_fact_blueprints) for case in self.conversations)
            != PACKAGE7_REQUIRED_FACT_COUNT
        ):
            raise ValueError("Evaluation v3 supported gold fact count has drifted")
        return self


class SplitEntryV3(FrozenGoldContractV3):
    conversation_id: IdentifierV3
    split: EvaluationSplitV3
    category: EvaluationCategoryV3
    work_group_id: IdentifierV3
    product_ids: tuple[int, ...]
    work_ids: tuple[IdentifierV3, ...]
    source_ids: tuple[IdentifierV3, ...]
    prompt_family_id: IdentifierV3
    scenario_family_id: IdentifierV3
    paraphrase_family_id: IdentifierV3 | None = None
    sandbox_fixture_id: IdentifierV3 | None
    sandbox_fixture_sha256: Sha256V3 | None
    leakage_keys: tuple[LeakageKeyV3, ...]


class SplitExclusionsV3(FrozenGoldContractV3):
    held_out_prior_v1_v2_product_ids: tuple[int, ...]
    held_out_development_probe_product_ids: tuple[int, ...]
    held_out_development_probe_work_ids: tuple[IdentifierV3, ...]
    held_out_development_probe_source_ids: tuple[IdentifierV3, ...]
    held_out_development_negative_families: tuple[IdentifierV3, ...]


class EvaluationSplitManifestV3(FrozenGoldContractV3):
    schema_version: Literal["3.0"]
    split_id: Literal["evaluation_v3_frozen_split"]
    corpus_id: IdentifierV3
    gold_sha256: Sha256V3
    source_identities_sha256: Sha256V3
    roster_sha256: Sha256V3
    category_order: tuple[EvaluationCategoryV3, ...]
    quota_plan: QuotaPlanV3
    frozen_pilot_ids: tuple[IdentifierV3, ...]
    exclusions: SplitExclusionsV3
    entries: tuple[SplitEntryV3, ...] = Field(min_length=80, max_length=80)

    @model_validator(mode="after")
    def validate_frozen_split(self) -> EvaluationSplitManifestV3:
        _validate_frozen_identity(
            self.category_order,
            self.quota_plan,
            self.frozen_pilot_ids,
        )
        _validate_split_entries(self.entries)
        expected_exclusions = (
            PACKAGE7_PRIOR_EXPOSED_PRODUCT_IDS,
            PACKAGE7_DEVELOPMENT_PRODUCT_IDS,
            PACKAGE7_DEVELOPMENT_WORK_IDS,
            PACKAGE7_DEVELOPMENT_SOURCE_IDS,
            PACKAGE7_DEVELOPMENT_NEGATIVE_FAMILIES,
        )
        actual_exclusions = (
            self.exclusions.held_out_prior_v1_v2_product_ids,
            self.exclusions.held_out_development_probe_product_ids,
            self.exclusions.held_out_development_probe_work_ids,
            self.exclusions.held_out_development_probe_source_ids,
            self.exclusions.held_out_development_negative_families,
        )
        if actual_exclusions != expected_exclusions:
            raise ValueError("held-out exclusion identities have drifted")
        blocked_products = set(PACKAGE7_PRIOR_EXPOSED_PRODUCT_IDS) | set(
            PACKAGE7_DEVELOPMENT_PRODUCT_IDS
        )
        for entry in self.entries:
            if entry.split != EvaluationSplitV3.HELD_OUT:
                continue
            if blocked_products & set(entry.product_ids):
                raise ValueError("held-out roster contains an exposed product")
            if set(PACKAGE7_DEVELOPMENT_WORK_IDS) & set(entry.work_ids):
                raise ValueError("held-out roster contains a development work")
            if set(PACKAGE7_DEVELOPMENT_SOURCE_IDS) & set(entry.source_ids):
                raise ValueError("held-out roster contains a development source")
        return self


@dataclass(frozen=True, slots=True)
class LoadedEvaluationGoldV3:
    gold: EvaluationGoldV3
    split: EvaluationSplitManifestV3
    gold_sha256: str
    split_sha256: str
    source_identities_sha256: str
    roster_sha256: str
    required_fact_count: int


def load_evaluation_gold_v3(
    gold_path: Path,
    split_path: Path,
    *,
    project_root: Path | None = None,
) -> LoadedEvaluationGoldV3:
    """Load, cross-bind, and independently validate all Evaluation v3 gold."""

    gold_payload = json.loads(gold_path.read_text(encoding="utf-8"))
    split_payload = json.loads(split_path.read_text(encoding="utf-8"))
    _reject_result_or_sut_provenance(gold_payload)
    _reject_result_or_sut_provenance(split_payload)
    gold = EvaluationGoldV3.model_validate(gold_payload)
    split = EvaluationSplitManifestV3.model_validate(split_payload)
    if gold.registry_contract_sha256 != registry_contract_sha256_v3():
        raise ValueError("frozen v2 capability registry contract has drifted")
    _validate_action_boundaries(gold)
    gold_sha256 = canonical_sha256(gold_payload)
    if split.gold_sha256 != gold_sha256:
        raise ValueError("split manifest gold hash mismatch")
    source_identities_sha256 = canonical_sha256(gold_payload["source_assets"])
    if split.source_identities_sha256 != source_identities_sha256:
        raise ValueError("split manifest source identity hash mismatch")
    roster_sha256 = canonical_sha256(split_payload["entries"])
    if split.roster_sha256 != roster_sha256:
        raise ValueError("split manifest roster hash mismatch")
    if split.corpus_id != gold.corpus_id:
        raise ValueError("split manifest references a different gold corpus")
    if split.category_order != gold.category_order:
        raise ValueError("split and gold category order differ")
    if split.quota_plan != gold.quota_plan:
        raise ValueError("split and gold quotas differ")
    if split.frozen_pilot_ids != gold.frozen_pilot_ids:
        raise ValueError("split and gold pilot rosters differ")
    expected_entries = tuple(
        _split_entry_from_gold(case) for case in gold.conversations
    )
    if split.entries != expected_entries:
        raise ValueError("split roster does not exactly match gold identities")

    root = project_root or gold_path.resolve().parents[2]
    assets = _load_and_validate_source_assets(gold.source_assets, root)
    required_fact_count = _validate_evidence_and_mappings(gold, assets)
    if required_fact_count != PACKAGE7_REQUIRED_FACT_COUNT:
        raise ValueError("resolved evidence count differs from frozen gold count")
    return LoadedEvaluationGoldV3(
        gold=gold,
        split=split,
        gold_sha256=gold_sha256,
        split_sha256=canonical_sha256(split_payload),
        source_identities_sha256=source_identities_sha256,
        roster_sha256=roster_sha256,
        required_fact_count=required_fact_count,
    )


def registry_contract_sha256_v3() -> str:
    """Hash the complete authorization-relevant default v2 registry surface."""

    registry = build_default_v2_registry()
    projection = [
        {
            "capability_id": definition.capability,
            "service": definition.service.value,
            "input_model": definition.input_model.__name__,
            "output_model": definition.output_model.__name__,
            "allowed_modes": sorted(mode.value for mode in definition.allowed_modes),
            "required_scopes": sorted(definition.required_permissions),
            "effect": definition.effect.value,
            "confirmation_policy": definition.confirmation_policy.value,
        }
        for definition in registry.list_capabilities()
    ]
    projection.sort(key=lambda item: str(item["capability_id"]))
    return canonical_sha256(projection)


def _capability_contract(capability_id: str) -> CapabilityContractV3:
    definition = build_default_v2_registry().capability(capability_id)
    return CapabilityContractV3(
        capability_id=definition.capability,
        allowed_modes=tuple(
            IdentityRoleV3(mode.value)
            for mode in sorted(definition.allowed_modes, key=lambda item: item.value)
        ),
        required_scopes=tuple(
            IdentityScopeV3(scope) for scope in sorted(definition.required_permissions)
        ),
        effect=definition.effect,
        confirmation_policy=definition.confirmation_policy,
    )


def _validate_action_boundaries(gold: EvaluationGoldV3) -> None:
    registry = build_default_v2_registry()
    registry_ids = {
        definition.capability for definition in registry.list_capabilities()
    }
    for case in gold.conversations:
        boundary = case.action_capability_blueprint
        allowed_ids = {item.capability_id for item in boundary.allowed}
        if allowed_ids | set(boundary.forbidden) != registry_ids:
            raise ValueError("action boundary does not partition the frozen registry")
        for item in (*boundary.allowed, *boundary.attempted):
            if item != _capability_contract(item.capability_id):
                raise ValueError("action capability metadata differs from v2 registry")
        mode = ConversationMode(case.identity_fixture.role.value)
        scopes = {scope.value for scope in case.identity_fixture.scopes}
        for item in boundary.allowed:
            definition = registry.capability(item.capability_id)
            if mode not in definition.allowed_modes:
                raise ValueError("allowed capability crosses shopper/merchant mode")
            if not definition.required_permissions.issubset(scopes):
                raise ValueError("allowed capability exceeds current identity scopes")
        if boundary.expected_outcome != ActionBoundaryOutcomeV3.DENIED:
            continue
        for item in boundary.attempted:
            definition = registry.capability(item.capability_id)
            authorized = (
                mode in definition.allowed_modes
                and definition.required_permissions.issubset(scopes)
            )
            unconfirmed_execute = (
                definition.effect == CapabilityEffect.EXECUTE
                and boundary.confirmation_state != ConfirmationStateV3.CONFIRMED
            )
            if authorized and not unconfirmed_execute:
                raise ValueError("denied capability has no authorization boundary")


def _validate_frozen_identity(
    category_order: tuple[EvaluationCategoryV3, ...],
    quota_plan: QuotaPlanV3,
    frozen_pilot_ids: tuple[str, ...],
) -> None:
    if tuple(category.value for category in category_order) != PACKAGE7_CATEGORY_ORDER:
        raise ValueError("Package 7 category order has drifted")
    if quota_plan.development.ordered_counts() != PACKAGE7_DEVELOPMENT_QUOTAS:
        raise ValueError("development category quotas have drifted")
    if quota_plan.development.total != 20:
        raise ValueError("development split must contain exactly 20 conversations")
    if quota_plan.held_out.ordered_counts() != PACKAGE7_HELDOUT_QUOTAS:
        raise ValueError("held-out category quotas have drifted")
    if quota_plan.held_out.total != 60:
        raise ValueError("held-out split must contain exactly 60 conversations")
    if frozen_pilot_ids != PACKAGE7_PILOT_CASE_ORDER:
        raise ValueError("frozen Package 7 pilot IDs have drifted")


def _validate_conversation_roster(
    conversations: tuple[GoldConversationV3, ...],
) -> None:
    ids = [case.conversation_id for case in conversations]
    if len(ids) != len(set(ids)):
        raise ValueError("gold conversation IDs must be unique")
    turns = [turn.turn_id for case in conversations for turn in case.user_turns]
    if len(turns) != len(set(turns)):
        raise ValueError("gold turn IDs must be globally unique")
    _validate_counts((case.split, case.category) for case in conversations)
    _validate_no_cross_split_groups(
        (
            case.split,
            case.work_group_id,
            case.leakage_keys,
        )
        for case in conversations
    )
    _validate_real_grouping(conversations)
    _validate_sandbox_fixture_repeats(conversations)
    known = {case.conversation_id: case for case in conversations}
    for pilot_id in PACKAGE7_PILOT_CASE_ORDER:
        if known[pilot_id].split != EvaluationSplitV3.DEVELOPMENT:
            raise ValueError("pilot conversations must belong to development")


def _validate_sandbox_fixture_repeats(
    conversations: tuple[GoldConversationV3, ...],
) -> None:
    fingerprints: dict[str, str] = {}
    for case in conversations:
        if case.sandbox_fixture is None:
            continue
        fingerprint = canonical_sha256(case.sandbox_fixture.model_dump(mode="json"))
        fixture_id = case.sandbox_fixture.fixture_id
        if fixture_id in fingerprints:
            raise ValueError("sandbox fixture IDs must be unique per conversation")
        fingerprints[fixture_id] = fingerprint


def _validate_split_entries(entries: tuple[SplitEntryV3, ...]) -> None:
    ids = [entry.conversation_id for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("split conversation IDs must be unique")
    _validate_counts((entry.split, entry.category) for entry in entries)
    _validate_no_cross_split_groups(
        (entry.split, entry.work_group_id, entry.leakage_keys) for entry in entries
    )
    _validate_real_grouping(entries)


def _validate_counts(
    rows: Any,
) -> None:
    counts = Counter(rows)
    expected: dict[tuple[EvaluationSplitV3, EvaluationCategoryV3], int] = {}
    for category, development, held_out in zip(
        EvaluationCategoryV3,
        PACKAGE7_DEVELOPMENT_QUOTAS,
        PACKAGE7_HELDOUT_QUOTAS,
        strict=True,
    ):
        expected[(EvaluationSplitV3.DEVELOPMENT, category)] = development
        expected[(EvaluationSplitV3.HELD_OUT, category)] = held_out
    if counts != expected:
        raise ValueError("Evaluation v3 split/category counts differ from Package 7")


def _validate_no_cross_split_groups(rows: Any) -> None:
    owners: dict[tuple[str, str], set[EvaluationSplitV3]] = defaultdict(set)
    for split, work_group_id, leakage_keys in rows:
        owners[("work_group", work_group_id)].add(split)
        for key in leakage_keys:
            owners[(key.kind.value, key.value)].add(split)
    leaked = sorted(key for key, splits in owners.items() if len(splits) > 1)
    if leaked:
        raise ValueError(f"cross-split leakage groups detected: {leaked[:5]}")


def _validate_real_grouping(
    rows: tuple[GoldConversationV3, ...] | tuple[SplitEntryV3, ...],
) -> None:
    work_sets_by_group: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    groups_by_work_set: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for row in rows:
        work_set = tuple(sorted(row.work_ids))
        work_sets_by_group[row.work_group_id].add(work_set)
        if work_set:
            groups_by_work_set[work_set].add(row.work_group_id)
        if row.work_group_id == row.conversation_id:
            raise ValueError("work groups cannot be conversation-ID singletons")
        if _has_split_alias(row.work_group_id):
            raise ValueError("work-group IDs cannot encode split labels")
        if len(work_set) == 1 and row.work_group_id != work_set[0]:
            raise ValueError("single-work groups must use the bound work ID")
        if len(work_set) > 1 and not row.work_group_id.startswith("pair_"):
            raise ValueError("multi-work groups must use a semantic pair ID")
        for family_id in (
            row.prompt_family_id,
            row.scenario_family_id,
            row.paraphrase_family_id,
        ):
            if family_id is None:
                raise ValueError("every conversation requires a paraphrase family")
            if _has_split_alias(family_id):
                raise ValueError("semantic family IDs cannot encode split labels")
            if family_id == row.conversation_id:
                raise ValueError("semantic families cannot be conversation-ID aliases")
    if any(len(work_sets) != 1 for work_sets in work_sets_by_group.values()):
        raise ValueError("work-group IDs must bind exactly one work set")
    if any(len(groups) != 1 for groups in groups_by_work_set.values()):
        raise ValueError("the same work set cannot be split across work groups")
    _require_non_vacuous_groups(rows, "work_group_id")
    _require_non_vacuous_groups(rows, "prompt_family_id")
    _require_non_vacuous_groups(rows, "scenario_family_id")
    _require_non_vacuous_groups(rows, "paraphrase_family_id")


def _require_non_vacuous_groups(rows: tuple[Any, ...], field: str) -> None:
    counts = Counter(getattr(row, field) for row in rows)
    grouped_cases = sum(count for count in counts.values() if count > 1)
    if grouped_cases < (len(rows) * 3) // 4:
        raise ValueError(f"{field} grouping is vacuous or mostly singleton")


def _has_split_alias(value: str) -> bool:
    tokens = set(re.split(r"[_.-]+", value.casefold()))
    return bool(tokens & _SPLIT_ALIAS_TOKENS)


def _split_entry_from_gold(case: GoldConversationV3) -> SplitEntryV3:
    fixture = case.sandbox_fixture
    return SplitEntryV3(
        conversation_id=case.conversation_id,
        split=case.split,
        category=case.category,
        work_group_id=case.work_group_id,
        product_ids=case.product_ids,
        work_ids=case.work_ids,
        source_ids=case.source_ids,
        prompt_family_id=case.prompt_family_id,
        scenario_family_id=case.scenario_family_id,
        paraphrase_family_id=case.paraphrase_family_id,
        sandbox_fixture_id=fixture.fixture_id if fixture is not None else None,
        sandbox_fixture_sha256=(
            canonical_sha256(fixture.model_dump(mode="json"))
            if fixture is not None
            else None
        ),
        leakage_keys=case.leakage_keys,
    )


def _validate_semantic_validity(
    conversations: tuple[GoldConversationV3, ...],
) -> None:
    registry = build_default_v2_registry()
    registry_ids = {
        definition.capability for definition in registry.list_capabilities()
    }
    prompt_template_bindings: dict[
        str,
        set[tuple[EvaluationSplitV3, str, str]],
    ] = defaultdict(set)
    near_duplicate_groups: dict[
        tuple[EvaluationCategoryV3, str | None],
        list[tuple[GoldConversationV3, frozenset[str]]],
    ] = defaultdict(list)
    for case in conversations:
        prompt = "\n".join(turn.message for turn in case.user_turns)
        normalized_prompt = prompt.casefold()
        paraphrase_family_id = case.paraphrase_family_id
        if paraphrase_family_id is None:
            raise ValueError("every conversation requires a paraphrase family")
        prompt_template_bindings[_semantic_prompt_template(case)].add(
            (
                case.split,
                case.prompt_family_id,
                paraphrase_family_id,
            )
        )
        comparison_intent = (
            case.sandbox_fixture.target_capability_id
            if case.category == EvaluationCategoryV3.SHOPPING_MERCHANT
            and case.sandbox_fixture is not None
            else None
        )
        near_duplicate_groups[(case.category, comparison_intent)].append(
            (case, _semantic_prompt_tokens(case))
        )
        if any(
            fragment in normalized_prompt for fragment in _OUTCOME_LEAKAGE_FRAGMENTS
        ):
            raise ValueError("user prompt leaks the hidden expected outcome")
        if any(
            fragment in normalized_prompt for fragment in _INTERNAL_PROMPT_FRAGMENTS
        ):
            raise ValueError("user prompt exposes internal sandbox or rubric state")
        if case.category == EvaluationCategoryV3.SHOPPING_MERCHANT and (
            "phiên bản" in normalized_prompt
            or re.search(r"\b(?:cart|offer)_[a-z0-9_.-]+", normalized_prompt)
        ):
            raise ValueError("shopping prompt exposes internal resource fields")
        if any(capability_id in prompt for capability_id in registry_ids):
            raise ValueError("user prompt exposes a registry capability ID")

        boundary = case.action_capability_blueprint
        allowed_ids = {item.capability_id for item in boundary.allowed}
        for capability_id in boundary.required:
            if not _capability_has_gold_obligation(
                case,
                capability_id,
                normalized_prompt,
            ):
                message = (
                    "required capability has no prompt/gold obligation: "
                    f"{capability_id}"
                )
                raise ValueError(message)
        for fact in case.required_fact_blueprints:
            if isinstance(fact.support, SourceExcerptSupportV3):
                if "knowledge.retrieve" not in allowed_ids:
                    raise ValueError(
                        "source fact is not obtainable through an allowed capability"
                    )
                continue
            field = fact.support.json_pointer.removeprefix("/")
            if not any(
                field in _CATALOG_FIELDS_BY_CAPABILITY.get(capability_id, ())
                for capability_id in allowed_ids
            ):
                raise ValueError(
                    "catalog fact is not obtainable through an allowed capability"
                )

        if case.category == EvaluationCategoryV3.MULTI_EXPERT_COMPARE_RECOMMENDATION:
            required = set(boundary.required)
            if not {"product.compare", "knowledge.retrieve"}.issubset(required):
                raise ValueError(
                    "multi-expert cases require catalog comparison and knowledge"
                )
            if required & {"review.retrieve", "review.compare"}:
                raise ValueError(
                    "multi-expert cases cannot claim review use without review gold"
                )
            if len(case.product_ids) < 2 or "so sánh" not in normalized_prompt:
                raise ValueError("multi-expert prompt must request a real comparison")
            services = {
                registry.capability(capability_id).service for capability_id in required
            }
            if len(services) < 2:
                raise ValueError("multi-expert cases require two expert services")

        if "src_morisaki_nlv" in case.source_ids:
            if "chủ đề" in normalized_prompt or "topic" in normalized_prompt:
                raise ValueError("Morisaki source does not support a topic claim")
            for fact in case.required_fact_blueprints:
                if (
                    isinstance(fact.support, SourceExcerptSupportV3)
                    and fact.support.record_id == "src_morisaki_nlv"
                ):
                    blueprint = fact.claim_blueprint.casefold()
                    if "tác giả" not in blueprint or "dịch" not in blueprint:
                        raise ValueError(
                            "Morisaki claim must stay within author/translation support"
                        )

    for bindings in prompt_template_bindings.values():
        splits = {split for split, _, _ in bindings}
        if len(splits) > 1:
            raise ValueError("semantic prompt template overlaps across splits")
        prompt_families = {prompt_family for _, prompt_family, _ in bindings}
        paraphrase_families = {
            paraphrase_family for _, _, paraphrase_family in bindings
        }
        if len(prompt_families) > 1 or len(paraphrase_families) > 1:
            raise ValueError("semantic prompt template has divergent family IDs")

    for rows in near_duplicate_groups.values():
        for index, (left_case, left_tokens) in enumerate(rows):
            for right_case, right_tokens in rows[index + 1 :]:
                if left_case.split == right_case.split:
                    continue
                similarity = _semantic_template_similarity(left_tokens, right_tokens)
                if similarity >= _SEMANTIC_TEMPLATE_SIMILARITY_THRESHOLD:
                    raise ValueError(
                        "near-duplicate semantic prompt template overlaps across "
                        f"splits: {left_case.conversation_id} / "
                        f"{right_case.conversation_id} ({similarity:.3f})"
                    )


def _semantic_prompt_template(case: GoldConversationV3) -> str:
    template = "\n".join(turn.message for turn in case.user_turns).casefold()
    product_names = {
        str(fact.expected_value).casefold()
        for fact in case.required_fact_blueprints
        if isinstance(fact.support, CatalogPointerSupportV3)
        and fact.support.json_pointer == "/name"
    }
    for product_name in sorted(product_names, key=len, reverse=True):
        template = template.replace(product_name, "<entity>")
    template = re.sub(r'[“"][^”"]+[”"]', "<entity>", template)
    template = re.sub(r"\d+(?:[.,]\d+)*", "<number>", template)
    return " ".join(template.split())


def _semantic_prompt_tokens(case: GoldConversationV3) -> frozenset[str]:
    template = _semantic_prompt_template(case)
    for pattern in _PROMPT_FILLER_PATTERNS:
        template = re.sub(pattern, " ", template)
    return frozenset(
        re.findall(r"<entity>|<number>|[^\W\d_]+", template, flags=re.UNICODE)
    )


def _semantic_template_similarity(
    left: frozenset[str],
    right: frozenset[str],
) -> float:
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def _capability_has_gold_obligation(
    case: GoldConversationV3,
    capability_id: str,
    normalized_prompt: str,
) -> bool:
    supports = tuple(fact.support for fact in case.required_fact_blueprints)
    has_catalog_fact = any(
        isinstance(support, CatalogPointerSupportV3) for support in supports
    )
    has_source_fact = any(
        isinstance(support, SourceExcerptSupportV3) for support in supports
    )
    if capability_id == "knowledge.retrieve":
        return has_source_fact
    if capability_id in {"product.catalog.search", "product.rank"}:
        return has_catalog_fact or (
            case.category == EvaluationCategoryV3.INSUFFICIENT_CONFLICT_INJECTION
            and "tìm" in normalized_prompt
        )
    if capability_id == "product.compare":
        return len(case.product_ids) >= 2 and "so sánh" in normalized_prompt
    fixture = case.sandbox_fixture
    if capability_id == "shopper.cart.read":
        return fixture is not None and fixture.cart is not None
    if capability_id == "shopper.checkout.preview":
        return fixture is not None and fixture.cart is not None
    if capability_id == "shopper.checkout.propose":
        return fixture is not None and isinstance(
            fixture.proposal_parameters,
            SandboxCheckoutProposalV3,
        )
    if capability_id == "merchant.inventory.read":
        return fixture is not None and fixture.merchant is not None
    if capability_id == "merchant.offer.propose":
        return fixture is not None and isinstance(
            fixture.proposal_parameters,
            SandboxOfferProposalV3,
        )
    return False


def _reject_result_or_sut_provenance(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).casefold()
            if normalized_key in _BLOCKED_PROVENANCE_KEYS:
                raise ValueError(f"forbidden SUT/result provenance field: {key}")
            _reject_result_or_sut_provenance(child)
        return
    if isinstance(value, list):
        for child in value:
            _reject_result_or_sut_provenance(child)
        return
    if isinstance(value, str):
        normalized = value.casefold()
        if any(fragment in normalized for fragment in _BLOCKED_PROVENANCE_FRAGMENTS):
            raise ValueError(
                "gold and split assets cannot reference SUT/result artifacts"
            )


def _load_and_validate_source_assets(
    bindings: SourceAssetsV3,
    project_root: Path,
) -> dict[str, object]:
    root = project_root.resolve()
    loaded: dict[str, object] = {}
    for name, binding in bindings:
        path = _resolve_asset_path(root, binding.path)
        if _sha256_file(path) != binding.sha256:
            raise ValueError(f"immutable source hash mismatch: {name}")
        if path.suffix == ".jsonl":
            records: object = tuple(
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            )
        else:
            records = json.loads(path.read_text(encoding="utf-8"))
        if binding.records is not None:
            count = _source_record_count(name, records)
            if count != binding.records:
                raise ValueError(f"immutable source record count mismatch: {name}")
        loaded[name] = records
    return loaded


def _resolve_asset_path(project_root: Path, relative_path: str) -> Path:
    candidate = (project_root / relative_path).resolve()
    if not candidate.is_relative_to(project_root):
        raise ValueError("source asset path escapes the project root")
    if not candidate.is_file():
        raise ValueError(f"source asset is missing: {relative_path}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_record_count(name: str, value: object) -> int:
    if isinstance(value, tuple):
        return len(value)
    if not isinstance(value, dict):
        raise ValueError(f"source asset has an invalid document shape: {name}")
    collection_name = {"sources": "sources", "mappings": "mappings"}.get(name)
    if collection_name is None or not isinstance(value.get(collection_name), list):
        raise ValueError(f"source asset has no countable records: {name}")
    return len(value[collection_name])


def _validate_evidence_and_mappings(
    gold: EvaluationGoldV3,
    assets: dict[str, object],
) -> int:
    sources_document = assets["sources"]
    mappings_document = assets["mappings"]
    products = assets["products"]
    if not isinstance(sources_document, dict):
        raise ValueError("source manifest must be a JSON object")
    if not isinstance(mappings_document, dict):
        raise ValueError("mapping manifest must be a JSON object")
    if not isinstance(products, tuple):
        raise ValueError("product snapshot must be JSONL records")
    _validate_sandbox_fixtures_against_catalog(gold, products)
    sources = sources_document.get("sources")
    mappings = mappings_document.get("mappings")
    if not isinstance(sources, list) or not isinstance(mappings, list):
        raise ValueError("knowledge manifests have invalid record collections")
    mapping_by_product = {
        record["product_id"]: record for record in mappings if isinstance(record, dict)
    }
    resolved = 0
    for case in gold.conversations:
        for product_id, work_id, source_id in zip(
            case.product_ids,
            case.work_ids,
            case.source_ids,
            strict=True,
        ):
            mapping = mapping_by_product.get(product_id)
            if not isinstance(mapping, dict):
                raise ValueError(f"missing product mapping for {product_id}")
            if mapping.get("mapping_status") != "exact_work":
                raise ValueError("gold knowledge facts require exact-work mappings")
            if mapping.get("work_identifier") != work_id:
                raise ValueError("gold work ID differs from immutable mapping")
            if mapping.get("source_ids") != [source_id]:
                raise ValueError("gold source ID differs from immutable mapping")
        for fact in case.required_fact_blueprints:
            support = fact.support
            if isinstance(support, SourceExcerptSupportV3):
                _validate_source_excerpt(
                    fact,
                    support,
                    sources_document,
                    sources,
                    gold.source_assets.sources.sha256,
                )
            else:
                _validate_catalog_pointer(
                    fact,
                    support,
                    products,
                    case.product_ids,
                    gold.source_assets.products.sha256,
                )
            resolved += 1
    return resolved


def _validate_sandbox_fixtures_against_catalog(
    gold: EvaluationGoldV3,
    products: tuple[object, ...],
) -> None:
    for case in gold.conversations:
        fixture = case.sandbox_fixture
        if fixture is None:
            continue
        messages = "\n".join(turn.message for turn in case.user_turns)
        prices_by_product: dict[int, int] = {}
        for product_id in case.product_ids:
            record = products[product_id - 1]
            if not isinstance(record, dict):
                raise ValueError("sandbox product record must be a JSON object")
            name = record.get("name")
            price = record.get("price_vnd")
            if not isinstance(name, str) or name not in messages:
                raise ValueError("shopping prompt omits a bound product name")
            if not isinstance(price, int):
                raise ValueError("sandbox product record has no integer price")
            prices_by_product[product_id] = price
        if fixture.cart is not None:
            for line in fixture.cart.lines:
                if line.unit_price_vnd != prices_by_product[line.product_id]:
                    raise ValueError("sandbox cart price differs from catalog snapshot")
        if fixture.merchant is not None:
            for offer in fixture.merchant.offers:
                if offer.price_vnd != prices_by_product[offer.product_id]:
                    raise ValueError(
                        "sandbox offer price differs from catalog snapshot"
                    )
        proposal = fixture.proposal_parameters
        required_tokens: tuple[str, ...] = ()
        if isinstance(proposal, SandboxOfferProposalV3):
            required_tokens = (
                f"{proposal.new_price_vnd:,}".replace(",", ".") + " đồng",
            )
        if any(token not in messages for token in required_tokens):
            raise ValueError("shopping prompt omits frozen action parameters")


def _validate_source_excerpt(
    fact: RequiredFactBlueprintV3,
    support: SourceExcerptSupportV3,
    source_document: dict[str, object],
    sources: list[object],
    expected_artifact_sha256: str,
) -> None:
    if support.artifact_sha256 != expected_artifact_sha256:
        raise ValueError("source evidence uses an unbound artifact hash")
    content = _resolve_json_pointer(source_document, support.json_pointer)
    if not isinstance(content, str):
        raise ValueError("source evidence pointer must resolve to text")
    index = int(support.json_pointer.split("/")[2])
    record = sources[index]
    if not isinstance(record, dict) or record.get("source_id") != support.record_id:
        raise ValueError("source evidence record ID mismatch")
    if record.get("support_scope") != support.support_scope:
        raise ValueError("source evidence exceeds its declared support scope")
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if content_sha256 != support.record_content_sha256:
        raise ValueError("source evidence content hash mismatch")
    if record.get("content_sha256") != content_sha256:
        raise ValueError("source manifest content hash mismatch")
    if content[support.start : support.end] != support.exact_excerpt:
        raise ValueError("source evidence span/excerpt mismatch")
    if fact.expected_value != support.exact_excerpt:
        raise ValueError("source fact value must equal its exact evidence excerpt")


def _validate_catalog_pointer(
    fact: RequiredFactBlueprintV3,
    support: CatalogPointerSupportV3,
    products: tuple[object, ...],
    case_product_ids: tuple[int, ...],
    expected_artifact_sha256: str,
) -> None:
    if support.artifact_sha256 != expected_artifact_sha256:
        raise ValueError("catalog evidence uses an unbound artifact hash")
    if support.record_line_1based not in case_product_ids:
        raise ValueError("catalog evidence references a product outside the case")
    record = products[support.record_line_1based - 1]
    value = _resolve_json_pointer(record, support.json_pointer)
    if value != support.expected_value or fact.expected_value != value:
        raise ValueError("catalog evidence pointer/value mismatch")


def _resolve_json_pointer(document: object, pointer: str) -> object:
    if not pointer.startswith("/"):
        raise ValueError("evidence JSON pointer must be absolute")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValueError(f"evidence JSON pointer key is absent: {token}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise ValueError("evidence JSON pointer index is invalid")
            current = current[int(token)]
        else:
            raise ValueError("evidence JSON pointer traverses a scalar")
    return current
