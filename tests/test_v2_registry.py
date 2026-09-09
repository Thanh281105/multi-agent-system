"""Typed v2 capability registry and v1 freeze regression tests."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.registry import AgentBundle, default_registry
from app.v2.contracts import ConversationMode
from app.v2.registry import (
    ActionExecutionResult,
    CapabilityEffect,
    CapabilityNotFoundError,
    CatalogSearchInput,
    ConfirmationPolicy,
    IntentTemplate,
    KnowledgeResult,
    MerchantOfferProposalInput,
    ProductResult,
    ProductSelectionInput,
    ServiceId,
    TrustFinding,
    V2CapabilityRegistry,
    build_default_v2_registry,
    default_v2_registry,
)


def test_default_registry_exposes_all_service_boundaries() -> None:
    assert default_v2_registry.services() == (
        ServiceId.PRODUCT,
        ServiceId.REVIEW,
        ServiceId.TRUST,
        ServiceId.MARKET,
        ServiceId.KNOWLEDGE,
        ServiceId.MERCHANT,
        ServiceId.SHOPPER_ACTION,
        ServiceId.MERCHANT_ACTION,
    )


def test_mode_specific_templates_are_fixed_and_bounded() -> None:
    shopper = {
        template.intent: template.capabilities
        for template in default_v2_registry.templates_for_mode(ConversationMode.SHOPPER)
    }
    merchant = {
        template.intent: template.capabilities
        for template in default_v2_registry.templates_for_mode(
            ConversationMode.MERCHANT
        )
    }

    assert shopper == {
        "catalog": ("product.catalog.search",),
        "compare": ("product.catalog.search", "product.compare"),
        "recommendation": (
            "product.rank",
            "review.compare",
            "trust.compare",
        ),
        "knowledge": ("knowledge.retrieve",),
        "catalog_knowledge": (
            "product.catalog.search",
            "knowledge.retrieve",
        ),
        "cart.read": ("shopper.cart.read",),
        "checkout.proposal": (
            "shopper.cart.read",
            "shopper.checkout.preview",
            "shopper.checkout.propose",
        ),
    }
    assert merchant == {
        "catalog": ("product.catalog.search",),
        "compare": ("product.catalog.search", "product.compare"),
        "recommendation": (
            "product.rank",
            "review.compare",
            "trust.compare",
        ),
        "knowledge": ("knowledge.retrieve",),
        "catalog_knowledge": (
            "product.catalog.search",
            "knowledge.retrieve",
        ),
        "merchant.read": (
            "merchant.catalog.read",
            "merchant.inventory.read",
        ),
        "merchant.proposal": (
            "merchant.inventory.read",
            "merchant.offer.propose",
        ),
    }


def test_capabilities_declare_types_permissions_effects_and_confirmation() -> None:
    catalog = default_v2_registry.capability("product.catalog.search")
    proposal = default_v2_registry.capability("merchant.offer.propose")
    checkout = default_v2_registry.capability("shopper.checkout.execute")

    assert catalog.input_model is CatalogSearchInput
    assert catalog.output_model is ProductResult
    assert catalog.effect == CapabilityEffect.READ
    assert catalog.required_permissions == frozenset({"ecommerce.read"})
    assert catalog.confirmation_policy == ConfirmationPolicy.NOT_REQUIRED

    assert proposal.effect == CapabilityEffect.PROPOSAL
    assert proposal.required_permissions == frozenset(
        {"ecommerce.read", "merchant.read", "merchant.write"}
    )
    assert proposal.confirmation_policy == ConfirmationPolicy.REQUIRED
    assert proposal.allowed_modes == frozenset({ConversationMode.MERCHANT})

    assert checkout.output_model is ActionExecutionResult
    assert checkout.effect == CapabilityEffect.EXECUTE
    assert checkout.confirmation_policy == ConfirmationPolicy.REQUIRED


def test_registry_validates_typed_inputs_and_rejects_authority_fields() -> None:
    catalog = default_v2_registry.capability("product.catalog.search")
    parsed = catalog.validate_input({"query": "sách lịch sử", "max_price_vnd": 200_000})
    assert isinstance(parsed, CatalogSearchInput)
    assert parsed.candidate_limit == 5
    assert CatalogSearchInput(max_price_vnd=200_000).max_price_vnd == 200_000

    with pytest.raises(ValidationError):
        catalog.validate_input({"query": "sách", "max_price_vnd": "200000"})
    with pytest.raises(ValidationError):
        catalog.validate_input({"query": "sách", "tenant_id": "foreign-tenant"})
    with pytest.raises(ValidationError):
        catalog.validate_input({"query": "sách", "candidate_limit": 6})


def test_product_ids_match_the_existing_numeric_catalog_boundary() -> None:
    selection = ProductSelectionInput(product_ids=(113731, 284929))
    assert selection.product_ids == (113731, 284929)

    with pytest.raises(ValidationError):
        ProductSelectionInput(product_ids=("113731",))
    with pytest.raises(ValidationError):
        ProductSelectionInput(product_ids=(0,))


def test_knowledge_and_trust_outputs_preserve_safety_meaning() -> None:
    with pytest.raises(ValidationError, match="requires at least one excerpt"):
        KnowledgeResult(
            corpus_version_id="corpus_00000001",
            excerpts=(),
            answerable=True,
        )
    with pytest.raises(ValidationError):
        TrustFinding(
            product_id=113731,
            complaint_count=0,
            heuristic_only=False,
            summary="No sampled complaint signal.",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "offer_id": "offer_00000001",
            "expected_version": 0,
            "new_price_vnd": 100_000,
        },
        {
            "offer_id": "offer_00000001",
            "expected_version": 1,
            "new_price_vnd": 0,
        },
        {
            "offer_id": "offer_00000001",
            "expected_version": 1,
            "quantity_delta": 0,
        },
        {
            "offer_id": "offer_00000001",
            "expected_version": 1,
            "new_price_vnd": 100_000,
            "quantity_delta": 1,
        },
        {
            "offer_id": "offer_00000001",
            "expected_version": 1,
            "new_price_vnd": 100_000,
            "store_id": "foreign-store",
        },
    ],
)
def test_merchant_proposal_validates_price_quantity_version_and_authority(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        MerchantOfferProposalInput.model_validate(payload)


def test_registry_rejects_unknown_or_unsafe_templates() -> None:
    capability = default_v2_registry.capability("product.catalog.search")

    with pytest.raises(ValueError, match="unknown capability"):
        V2CapabilityRegistry(
            (capability,),
            (
                IntentTemplate(
                    template_id="bad_unknown",
                    mode=ConversationMode.SHOPPER,
                    intent="bad.unknown",
                    capabilities=("unknown.read",),
                ),
            ),
        )

    merchant_only = default_v2_registry.capability("merchant.catalog.read")
    with pytest.raises(ValueError, match="unavailable"):
        V2CapabilityRegistry(
            (merchant_only,),
            (
                IntentTemplate(
                    template_id="bad_mode",
                    mode=ConversationMode.SHOPPER,
                    intent="bad.mode",
                    capabilities=(merchant_only.capability,),
                ),
            ),
        )

    execute = default_v2_registry.capability("shopper.checkout.execute")
    with pytest.raises(ValueError, match="cannot execute"):
        V2CapabilityRegistry(
            (execute,),
            (
                IntentTemplate(
                    template_id="bad_execute",
                    mode=ConversationMode.SHOPPER,
                    intent="bad.execute",
                    capabilities=(execute.capability,),
                ),
            ),
        )


def test_registry_rejects_duplicate_capabilities_and_missing_permissions() -> None:
    capability = default_v2_registry.capability("product.catalog.search")
    template = IntentTemplate(
        template_id="shopper_catalog",
        mode=ConversationMode.SHOPPER,
        intent="catalog",
        capabilities=(capability.capability,),
    )
    with pytest.raises(ValueError, match="duplicate v2 capability"):
        V2CapabilityRegistry((capability, capability), (template,))

    with pytest.raises(ValueError, match="no required permission"):
        V2CapabilityRegistry(
            (replace(capability, required_permissions=frozenset()),),
            (template,),
        )


def test_missing_capability_has_a_stable_lookup_error() -> None:
    with pytest.raises(CapabilityNotFoundError):
        default_v2_registry.capability("not.registered")


def test_building_v2_registry_does_not_mutate_frozen_v1_registry() -> None:
    before_json = tuple(bundle.model_dump_json() for bundle in default_registry.list())
    before_schema = AgentBundle.model_json_schema()
    expected_v1_agents = (
        "orchestrator",
        "product_agent",
        "review_agent",
        "trust_agent",
        "market_agent",
    )
    expected_v1_fields = {
        "agent_id",
        "version",
        "description",
        "capabilities",
        "skills",
        "permissions",
        "mcp_servers",
        "rate_limit",
        "follow_up_intent",
    }

    rebuilt = build_default_v2_registry()

    assert rebuilt is not default_v2_registry
    assert tuple(bundle.agent_id for bundle in default_registry.list()) == (
        expected_v1_agents
    )
    assert set(before_schema["properties"]) == expected_v1_fields
    assert tuple(bundle.model_dump_json() for bundle in default_registry.list()) == (
        before_json
    )
    assert AgentBundle.model_json_schema() == before_schema
