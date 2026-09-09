"""Isolated capability catalog for v2 planning and service authorization."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import Field, model_validator

from app.v2.contracts import (
    IDENTIFIER_PATTERN,
    MAX_CANDIDATES,
    MAX_EXPERT_STEPS,
    ActionCard,
    ActionStatus,
    ConversationMode,
    EvidenceReference,
    PositiveQuantity,
    PriceVnd,
    ProductId,
    Quantity,
    ResourceVersion,
    StableId,
    V2Contract,
)


class CapabilityEffect(StrEnum):
    READ = "read"
    PROPOSAL = "proposal"
    EXECUTE = "execute"


class ConfirmationPolicy(StrEnum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"


class ServiceId(StrEnum):
    PRODUCT = "product"
    REVIEW = "review"
    TRUST = "trust"
    MARKET = "market"
    KNOWLEDGE = "knowledge"
    MERCHANT = "merchant"
    SHOPPER_ACTION = "shopper_action"
    MERCHANT_ACTION = "merchant_action"


class CatalogSearchInput(V2Contract):
    query: str | None = Field(default=None, min_length=1, max_length=300)
    author: str | None = Field(default=None, min_length=1, max_length=160)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    publisher: str | None = Field(default=None, min_length=1, max_length=160)
    min_price_vnd: PriceVnd | None = None
    max_price_vnd: PriceVnd | None = None
    candidate_limit: int = Field(
        default=MAX_CANDIDATES,
        strict=True,
        ge=1,
        le=MAX_CANDIDATES,
    )

    @model_validator(mode="after")
    def validate_search(self) -> CatalogSearchInput:
        if not any(
            (
                self.query,
                self.author,
                self.category,
                self.publisher,
                self.min_price_vnd is not None,
                self.max_price_vnd is not None,
            )
        ):
            raise ValueError("catalog search requires a query or catalog filter")
        if (
            self.min_price_vnd is not None
            and self.max_price_vnd is not None
            and self.min_price_vnd > self.max_price_vnd
        ):
            raise ValueError("minimum price cannot exceed maximum price")
        return self


class ProductSelectionInput(V2Contract):
    product_ids: tuple[ProductId, ...] = Field(min_length=1, max_length=MAX_CANDIDATES)

    @model_validator(mode="after")
    def validate_products(self) -> ProductSelectionInput:
        if len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("product IDs must be unique")
        return self


class ProductCandidate(V2Contract):
    product_id: ProductId
    title: str = Field(min_length=1, max_length=300)
    author: str | None = Field(default=None, min_length=1, max_length=160)
    price_vnd: PriceVnd | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    catalog_version_id: str = Field(pattern=IDENTIFIER_PATTERN)


class ProductResult(V2Contract):
    products: tuple[ProductCandidate, ...] = Field(max_length=MAX_CANDIDATES)
    evidence: tuple[EvidenceReference, ...] = ()


class ReviewFinding(V2Contract):
    product_id: ProductId
    review_count: int = Field(strict=True, ge=0)
    average_rating: float | None = Field(default=None, ge=0, le=5)
    summary: str = Field(min_length=1, max_length=1_000)
    evidence_ids: tuple[StableId, ...] = ()


class ReviewResult(V2Contract):
    findings: tuple[ReviewFinding, ...] = Field(max_length=MAX_CANDIDATES)


class TrustFinding(V2Contract):
    product_id: ProductId
    complaint_count: int = Field(strict=True, ge=0)
    heuristic_only: Literal[True] = True
    summary: str = Field(min_length=1, max_length=1_000)
    evidence_ids: tuple[StableId, ...] = ()


class TrustResult(V2Contract):
    findings: tuple[TrustFinding, ...] = Field(max_length=MAX_CANDIDATES)


class MarketDimension(StrEnum):
    CATEGORY = "category"
    AUTHOR = "author"
    PUBLISHER = "publisher"
    PRICE = "price"
    RATING = "rating"


class MarketSnapshotInput(V2Contract):
    dimension: MarketDimension
    category: str | None = Field(default=None, min_length=1, max_length=120)
    author: str | None = Field(default=None, min_length=1, max_length=160)


class MarketMetric(V2Contract):
    label: str = Field(min_length=1, max_length=160)
    count: int = Field(strict=True, ge=0)
    value: Decimal | None = Field(default=None, ge=0)


class MarketResult(V2Contract):
    snapshot_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    metrics: tuple[MarketMetric, ...]


class KnowledgeRetrieveInput(V2Contract):
    query: str = Field(min_length=1, max_length=500)
    product_ids: tuple[ProductId, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    requested_source_ids: frozenset[StableId] | None = None
    top_k: int = Field(default=6, strict=True, ge=1, le=8)

    @model_validator(mode="after")
    def validate_knowledge_input(self) -> KnowledgeRetrieveInput:
        if len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("product IDs must be unique")
        if self.requested_source_ids is not None and any(
            not source_id for source_id in self.requested_source_ids
        ):
            raise ValueError("requested source IDs cannot be blank")
        return self


class KnowledgeExcerpt(V2Contract):
    evidence_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    chunk_id: str = Field(pattern=IDENTIFIER_PATTERN)
    span_id: str = Field(pattern=IDENTIFIER_PATTERN)
    excerpt: str = Field(min_length=1, max_length=2_000)
    score: float = Field(ge=0)


class KnowledgeResult(V2Contract):
    corpus_version_id: str = Field(pattern=IDENTIFIER_PATTERN)
    excerpts: tuple[KnowledgeExcerpt, ...] = Field(max_length=8)
    answerable: bool

    @model_validator(mode="after")
    def validate_answerability(self) -> KnowledgeResult:
        if self.answerable and not self.excerpts:
            raise ValueError("answerable knowledge requires at least one excerpt")
        return self


class MerchantReadInput(V2Contract):
    product_ids: tuple[ProductId, ...] = Field(default=(), max_length=MAX_CANDIDATES)

    @model_validator(mode="after")
    def validate_products(self) -> MerchantReadInput:
        if len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("product IDs must be unique")
        return self


class MerchantOffer(V2Contract):
    offer_id: str = Field(pattern=IDENTIFIER_PATTERN)
    product_id: ProductId
    price_vnd: PriceVnd
    available_quantity: Quantity
    version: ResourceVersion


class MerchantReadResult(V2Contract):
    offers: tuple[MerchantOffer, ...] = Field(max_length=MAX_CANDIDATES)
    snapshot_version_id: str = Field(pattern=IDENTIFIER_PATTERN)


class MerchantOfferProposalInput(V2Contract):
    offer_id: str = Field(pattern=IDENTIFIER_PATTERN)
    expected_version: ResourceVersion
    new_price_vnd: PriceVnd | None = None
    quantity_delta: int | None = Field(
        default=None,
        strict=True,
        ge=-1_000_000,
        le=1_000_000,
    )

    @model_validator(mode="after")
    def validate_change(self) -> MerchantOfferProposalInput:
        if (self.new_price_vnd is None) == (self.quantity_delta is None):
            raise ValueError("proposal must change exactly one offer field")
        if self.quantity_delta == 0:
            raise ValueError("quantity delta cannot be zero")
        return self


class CartChangeInput(V2Contract):
    cart_id: str = Field(pattern=IDENTIFIER_PATTERN)
    product_id: ProductId
    quantity: Quantity
    expected_version: ResourceVersion


class CartReadInput(V2Contract):
    """Resolve the authenticated principal's current cart."""


class CartItem(V2Contract):
    product_id: ProductId
    title: str = Field(min_length=1, max_length=300)
    quantity: PositiveQuantity
    unit_price_vnd: PriceVnd
    line_total_vnd: PriceVnd

    @model_validator(mode="after")
    def validate_line_total(self) -> CartItem:
        if self.line_total_vnd != self.quantity * self.unit_price_vnd:
            raise ValueError("cart line total must equal quantity times unit price")
        return self


class CartResult(V2Contract):
    cart_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: ResourceVersion
    items: tuple[CartItem, ...]
    total_price_vnd: int = Field(strict=True, ge=0, le=10_000_000_000)


class CheckoutInput(V2Contract):
    cart_id: str = Field(pattern=IDENTIFIER_PATTERN)
    expected_version: ResourceVersion


class CheckoutPreviewResult(V2Contract):
    cart: CartResult
    can_checkout: bool
    issues: tuple[str, ...] = ()


class ActionExecuteInput(V2Contract):
    action_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposal_version: ResourceVersion
    idempotency_key: str = Field(min_length=8, max_length=128)


class ProposalResult(V2Contract):
    action: ActionCard


class ActionExecutionResult(V2Contract):
    action_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: ActionStatus
    resource_id: str = Field(pattern=IDENTIFIER_PATTERN)
    resource_version: ResourceVersion
    reused_result: bool = False


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """One typed service operation available to deterministic compilation."""

    capability: str
    service: ServiceId
    input_model: type[V2Contract]
    output_model: type[V2Contract]
    effect: CapabilityEffect
    required_permissions: frozenset[str]
    confirmation_policy: ConfirmationPolicy
    allowed_modes: frozenset[ConversationMode]

    def validate_input(self, payload: Mapping[str, object]) -> V2Contract:
        return self.input_model.model_validate(payload)

    def validate_output(self, payload: Mapping[str, object]) -> V2Contract:
        return self.output_model.model_validate(payload)


class IntentTemplate(V2Contract):
    template_id: str = Field(pattern=IDENTIFIER_PATTERN)
    mode: ConversationMode
    intent: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    capabilities: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_EXPERT_STEPS,
    )

    @model_validator(mode="after")
    def validate_capabilities(self) -> IntentTemplate:
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("intent capabilities must be unique")
        return self


class CapabilityNotFoundError(LookupError):
    pass


class IntentTemplateNotFoundError(LookupError):
    pass


class V2CapabilityRegistry:
    """Immutable v2 registry, independent of the frozen v1 agent registry."""

    def __init__(
        self,
        capabilities: Iterable[CapabilityDefinition],
        templates: Iterable[IntentTemplate],
    ) -> None:
        indexed_capabilities: dict[str, CapabilityDefinition] = {}
        for definition in capabilities:
            if definition.capability in indexed_capabilities:
                raise ValueError(f"duplicate v2 capability: {definition.capability}")
            if not definition.required_permissions:
                raise ValueError(
                    f"capability has no required permission: {definition.capability}"
                )
            if not definition.allowed_modes:
                raise ValueError(
                    f"capability has no allowed mode: {definition.capability}"
                )
            if (
                definition.effect == CapabilityEffect.READ
                and definition.confirmation_policy != ConfirmationPolicy.NOT_REQUIRED
            ):
                raise ValueError("read capabilities cannot require confirmation")
            indexed_capabilities[definition.capability] = definition

        indexed_templates: dict[tuple[ConversationMode, str], IntentTemplate] = {}
        for template in templates:
            key = (template.mode, template.intent)
            if key in indexed_templates:
                raise ValueError(
                    f"duplicate v2 intent template: {template.mode}/{template.intent}"
                )
            for capability in template.capabilities:
                try:
                    definition = indexed_capabilities[capability]
                except KeyError as exc:
                    raise ValueError(
                        f"intent template references unknown capability: {capability}"
                    ) from exc
                if template.mode not in definition.allowed_modes:
                    raise ValueError(
                        f"capability {capability} is unavailable in "
                        f"{template.mode} mode"
                    )
                if definition.effect == CapabilityEffect.EXECUTE:
                    raise ValueError(
                        "model-selectable intent templates cannot execute actions"
                    )
            indexed_templates[key] = template

        if not indexed_capabilities or not indexed_templates:
            raise ValueError("v2 registry requires capabilities and intent templates")
        self._capabilities = MappingProxyType(indexed_capabilities)
        self._templates = MappingProxyType(indexed_templates)

    def capability(self, capability: str) -> CapabilityDefinition:
        try:
            return self._capabilities[capability]
        except KeyError as exc:
            raise CapabilityNotFoundError(capability) from exc

    def list_capabilities(self) -> tuple[CapabilityDefinition, ...]:
        return tuple(self._capabilities.values())

    def services(self) -> tuple[ServiceId, ...]:
        return tuple(
            dict.fromkeys(item.service for item in self._capabilities.values())
        )

    def template(self, mode: ConversationMode, intent: str) -> IntentTemplate:
        try:
            return self._templates[(mode, intent)]
        except KeyError as exc:
            raise IntentTemplateNotFoundError(f"{mode}/{intent}") from exc

    def templates_for_mode(self, mode: ConversationMode) -> tuple[IntentTemplate, ...]:
        return tuple(
            template
            for (template_mode, _), template in self._templates.items()
            if template_mode == mode
        )


_SHOPPER = frozenset({ConversationMode.SHOPPER})
_MERCHANT = frozenset({ConversationMode.MERCHANT})
_BOTH = frozenset({ConversationMode.SHOPPER, ConversationMode.MERCHANT})
_ECOMMERCE_READ = frozenset({"ecommerce.read"})
_MERCHANT_READ = frozenset({"ecommerce.read", "merchant.read"})
_SHOPPER_WRITE = frozenset({"ecommerce.read", "ecommerce.write"})
_MERCHANT_WRITE = frozenset({"ecommerce.read", "merchant.read", "merchant.write"})


def _capability(
    capability: str,
    service: ServiceId,
    input_model: type[V2Contract],
    output_model: type[V2Contract],
    *,
    effect: CapabilityEffect = CapabilityEffect.READ,
    permissions: frozenset[str] = _ECOMMERCE_READ,
    confirmation: ConfirmationPolicy = ConfirmationPolicy.NOT_REQUIRED,
    modes: frozenset[ConversationMode] = _BOTH,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability=capability,
        service=service,
        input_model=input_model,
        output_model=output_model,
        effect=effect,
        required_permissions=permissions,
        confirmation_policy=confirmation,
        allowed_modes=modes,
    )


def build_default_v2_registry() -> V2CapabilityRegistry:
    """Build the fixed Package 2 catalog without touching the v1 registry."""

    capabilities = (
        _capability(
            "product.catalog.search",
            ServiceId.PRODUCT,
            CatalogSearchInput,
            ProductResult,
        ),
        _capability(
            "product.compare",
            ServiceId.PRODUCT,
            ProductSelectionInput,
            ProductResult,
        ),
        _capability(
            "product.rank",
            ServiceId.PRODUCT,
            CatalogSearchInput,
            ProductResult,
        ),
        _capability(
            "review.retrieve",
            ServiceId.REVIEW,
            ProductSelectionInput,
            ReviewResult,
        ),
        _capability(
            "review.compare",
            ServiceId.REVIEW,
            ProductSelectionInput,
            ReviewResult,
        ),
        _capability(
            "trust.analyze",
            ServiceId.TRUST,
            ProductSelectionInput,
            TrustResult,
        ),
        _capability(
            "trust.compare",
            ServiceId.TRUST,
            ProductSelectionInput,
            TrustResult,
        ),
        _capability(
            "market.snapshot",
            ServiceId.MARKET,
            MarketSnapshotInput,
            MarketResult,
        ),
        _capability(
            "knowledge.retrieve",
            ServiceId.KNOWLEDGE,
            KnowledgeRetrieveInput,
            KnowledgeResult,
        ),
        _capability(
            "merchant.catalog.read",
            ServiceId.MERCHANT,
            MerchantReadInput,
            ProductResult,
            permissions=_MERCHANT_READ,
            modes=_MERCHANT,
        ),
        _capability(
            "merchant.inventory.read",
            ServiceId.MERCHANT,
            MerchantReadInput,
            MerchantReadResult,
            permissions=_MERCHANT_READ,
            modes=_MERCHANT,
        ),
        _capability(
            "merchant.offer.propose",
            ServiceId.MERCHANT,
            MerchantOfferProposalInput,
            ProposalResult,
            effect=CapabilityEffect.PROPOSAL,
            permissions=_MERCHANT_WRITE,
            confirmation=ConfirmationPolicy.REQUIRED,
            modes=_MERCHANT,
        ),
        _capability(
            "shopper.cart.read",
            ServiceId.SHOPPER_ACTION,
            CartReadInput,
            CartResult,
            modes=_SHOPPER,
        ),
        _capability(
            "shopper.checkout.preview",
            ServiceId.SHOPPER_ACTION,
            CheckoutInput,
            CheckoutPreviewResult,
            modes=_SHOPPER,
        ),
        _capability(
            "shopper.checkout.propose",
            ServiceId.SHOPPER_ACTION,
            CheckoutInput,
            ProposalResult,
            effect=CapabilityEffect.PROPOSAL,
            permissions=_SHOPPER_WRITE,
            confirmation=ConfirmationPolicy.REQUIRED,
            modes=_SHOPPER,
        ),
        _capability(
            "shopper.cart.execute",
            ServiceId.SHOPPER_ACTION,
            CartChangeInput,
            ActionExecutionResult,
            effect=CapabilityEffect.EXECUTE,
            permissions=_SHOPPER_WRITE,
            modes=_SHOPPER,
        ),
        _capability(
            "shopper.checkout.execute",
            ServiceId.SHOPPER_ACTION,
            ActionExecuteInput,
            ActionExecutionResult,
            effect=CapabilityEffect.EXECUTE,
            permissions=_SHOPPER_WRITE,
            confirmation=ConfirmationPolicy.REQUIRED,
            modes=_SHOPPER,
        ),
        _capability(
            "merchant.offer.execute",
            ServiceId.MERCHANT_ACTION,
            ActionExecuteInput,
            ActionExecutionResult,
            effect=CapabilityEffect.EXECUTE,
            permissions=_MERCHANT_WRITE,
            confirmation=ConfirmationPolicy.REQUIRED,
            modes=_MERCHANT,
        ),
    )
    templates = (
        IntentTemplate(
            template_id="shopper_catalog",
            mode=ConversationMode.SHOPPER,
            intent="catalog",
            capabilities=("product.catalog.search",),
        ),
        IntentTemplate(
            template_id="shopper_compare",
            mode=ConversationMode.SHOPPER,
            intent="compare",
            capabilities=("product.catalog.search", "product.compare"),
        ),
        IntentTemplate(
            template_id="shopper_recommendation",
            mode=ConversationMode.SHOPPER,
            intent="recommendation",
            capabilities=("product.rank", "review.compare", "trust.compare"),
        ),
        IntentTemplate(
            template_id="shopper_knowledge",
            mode=ConversationMode.SHOPPER,
            intent="knowledge",
            capabilities=("knowledge.retrieve",),
        ),
        IntentTemplate(
            template_id="shopper_catalog_knowledge",
            mode=ConversationMode.SHOPPER,
            intent="catalog_knowledge",
            capabilities=("product.catalog.search", "knowledge.retrieve"),
        ),
        IntentTemplate(
            template_id="shopper_cart_read",
            mode=ConversationMode.SHOPPER,
            intent="cart.read",
            capabilities=("shopper.cart.read",),
        ),
        IntentTemplate(
            template_id="shopper_checkout_proposal",
            mode=ConversationMode.SHOPPER,
            intent="checkout.proposal",
            capabilities=(
                "shopper.cart.read",
                "shopper.checkout.preview",
                "shopper.checkout.propose",
            ),
        ),
        IntentTemplate(
            template_id="merchant_catalog",
            mode=ConversationMode.MERCHANT,
            intent="catalog",
            capabilities=("product.catalog.search",),
        ),
        IntentTemplate(
            template_id="merchant_compare",
            mode=ConversationMode.MERCHANT,
            intent="compare",
            capabilities=("product.catalog.search", "product.compare"),
        ),
        IntentTemplate(
            template_id="merchant_recommendation",
            mode=ConversationMode.MERCHANT,
            intent="recommendation",
            capabilities=("product.rank", "review.compare", "trust.compare"),
        ),
        IntentTemplate(
            template_id="merchant_knowledge",
            mode=ConversationMode.MERCHANT,
            intent="knowledge",
            capabilities=("knowledge.retrieve",),
        ),
        IntentTemplate(
            template_id="merchant_catalog_knowledge",
            mode=ConversationMode.MERCHANT,
            intent="catalog_knowledge",
            capabilities=("product.catalog.search", "knowledge.retrieve"),
        ),
        IntentTemplate(
            template_id="merchant_read",
            mode=ConversationMode.MERCHANT,
            intent="merchant.read",
            capabilities=("merchant.catalog.read", "merchant.inventory.read"),
        ),
        IntentTemplate(
            template_id="merchant_proposal",
            mode=ConversationMode.MERCHANT,
            intent="merchant.proposal",
            capabilities=("merchant.inventory.read", "merchant.offer.propose"),
        ),
    )
    return V2CapabilityRegistry(capabilities, templates)


default_v2_registry = build_default_v2_registry()
