"""Real PostgreSQL checks for Package 5 sandbox read-tool wiring."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import TaskStatus
from app.db.migrate import upgrade_database
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.v2 import V2Cart, V2CartLine, V2Offer
from app.v2.actions import V2ActionService
from app.v2.authorization import (
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceBinding,
)
from app.v2.contracts import ConversationMode, EvidenceKind
from app.v2.registry import (
    CartResult,
    CheckoutPreviewResult,
    MerchantReadResult,
    ProductResult,
)
from app.v2.runtime_contracts import RuntimeOperation, build_operation_key
from app.v2.tools import CatalogSnapshot, V2ReadTools, load_catalog_snapshot
from tests.v2_postgres_support import disposable_postgres_database

OBSERVED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgres_read_fixture() -> Iterator[tuple[sessionmaker[Session], CatalogSnapshot]]:
    with disposable_postgres_database("thanh_v2_p2_read_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with sessions.begin() as session:
                _seed_catalog_and_sandbox(session)
            yield sessions, load_catalog_snapshot(sessions)
        finally:
            engine.dispose()


def test_shopper_catalog_filters_on_demo_price_and_keeps_snapshot_evidence(
    postgres_read_fixture: tuple[sessionmaker[Session], CatalogSnapshot],
) -> None:
    sessions, snapshot = postgres_read_fixture
    tools = _read_tools(sessions, snapshot)
    result = _run(
        tools,
        "product.catalog.search",
        {"query": "Historical book", "max_price_vnd": 100_000},
        _shopper_access(),
    )

    assert result.status == TaskStatus.SUCCESS
    output = ProductResult.model_validate(result.output)
    assert [(item.product_id, item.price_vnd) for item in output.products] == [
        (1, 42_000)
    ]
    assert {reference.kind for reference in result.evidence.references} == {
        EvidenceKind.CATALOG,
        EvidenceKind.SANDBOX,
    }
    snapshot_facts = {
        fact.field: fact.value
        for fact in result.evidence.facts
        if fact.field == "snapshot_price_vnd"
    }
    demo_facts = {
        fact.field: fact.value
        for fact in result.evidence.facts
        if fact.field == "demo_price_vnd"
    }
    assert snapshot_facts == {"snapshot_price_vnd": 500_000}
    assert demo_facts == {"demo_price_vnd": 42_000}
    assert any(
        reference.kind == EvidenceKind.CATALOG and "lịch sử" in reference.title
        for reference in result.evidence.references
    )
    assert any(
        reference.kind == EvidenceKind.SANDBOX and "demo price" in reference.title
        for reference in result.evidence.references
    )

    no_offer = _run(
        tools,
        "product.catalog.search",
        {"query": "No offer book"},
        _shopper_access(),
    )
    assert no_offer.status == TaskStatus.SUCCESS
    assert ProductResult.model_validate(no_offer.output).products == ()
    assert no_offer.evidence.references == ()


def test_inventory_cart_and_checkout_reads_are_sandbox_evidence(
    postgres_read_fixture: tuple[sessionmaker[Session], CatalogSnapshot],
) -> None:
    sessions, snapshot = postgres_read_fixture
    tools = _read_tools(sessions, snapshot)

    inventory = _run(
        tools,
        "merchant.inventory.read",
        {},
        _merchant_access(),
    )
    assert inventory.status == TaskStatus.SUCCESS
    inventory_output = MerchantReadResult.model_validate(inventory.output)
    assert [
        (offer.offer_id, offer.price_vnd, offer.available_quantity, offer.version)
        for offer in inventory_output.offers
    ] == [
        ("offer_read_one", 42_000, 7, 3),
        ("offer_read_two", 180_000, 10, 1),
    ]
    assert inventory.evidence.references
    assert all(
        reference.kind == EvidenceKind.SANDBOX and "Demo inventory" in reference.title
        for reference in inventory.evidence.references
    )

    cart = _run(tools, "shopper.cart.read", {}, _shopper_access())
    assert cart.status == TaskStatus.SUCCESS
    cart_output = CartResult.model_validate(cart.output)
    assert cart_output.cart_id == "cart_read"
    assert cart_output.total_price_vnd == 84_000
    assert cart_output.items[0].unit_price_vnd == 42_000
    assert cart.evidence.references
    assert all(
        reference.kind == EvidenceKind.SANDBOX and "Demo cart" in reference.title
        for reference in cart.evidence.references
    )

    checkout = _run(
        tools,
        "shopper.checkout.preview",
        {"cart_id": "cart_read", "expected_version": 1},
        _shopper_access(),
    )
    assert checkout.status == TaskStatus.SUCCESS
    checkout_output = CheckoutPreviewResult.model_validate(checkout.output)
    assert checkout_output.can_checkout is True
    assert checkout_output.cart.total_price_vnd == 84_000
    assert checkout.evidence.references
    assert all(
        reference.kind == EvidenceKind.SANDBOX
        for reference in checkout.evidence.references
    )
    assert {reference.title for reference in checkout.evidence.references} == {
        "Demo checkout preview — cart_read",
        "Demo checkout item — product 1 in cart_read",
    }
    assert {excerpt.subject_ids for excerpt in checkout.evidence.excerpts} == {
        ("cart_read",),
        ("product_1",),
    }


def test_sandbox_reads_enforce_current_owner_mode_and_service_boundary(
    postgres_read_fixture: tuple[sessionmaker[Session], CatalogSnapshot],
) -> None:
    sessions, snapshot = postgres_read_fixture
    tools = _read_tools(sessions, snapshot)
    foreign = _run(
        tools,
        "shopper.cart.read",
        {},
        _shopper_access(principal_id="foreign-principal"),
    )
    assert foreign.status == TaskStatus.FAILED
    assert foreign.error is not None and foreign.error.code == "resource_not_found"

    with pytest.raises(AuthorizationDeniedError):
        _run(
            tools,
            "shopper.cart.read",
            {},
            _merchant_access(),
        )

    no_service = V2ReadTools(sessions, catalog_snapshot=snapshot)
    unavailable = _run(
        no_service,
        "merchant.inventory.read",
        {},
        _merchant_access(),
    )
    assert unavailable.status == TaskStatus.FAILED
    assert (
        unavailable.error is not None
        and unavailable.error.code == "read_capability_unavailable"
    )


def _read_tools(
    sessions: sessionmaker[Session], snapshot: CatalogSnapshot
) -> V2ReadTools:
    return V2ReadTools(
        sessions,
        catalog_snapshot=snapshot,
        action_service=V2ActionService(
            sessions, catalog_version_id=snapshot.version_id
        ),
    )


def _run(
    tools: V2ReadTools,
    capability: str,
    parameters: dict[str, Any],
    access: ResourceAuthorization,
) -> Any:
    definition = tools.registry.capability(capability)
    canonical = definition.validate_input(parameters).model_dump(mode="json")
    versions = tools.data_version_ids(capability)
    operation = RuntimeOperation(
        step_id="step_sandbox_read",
        capability=capability,
        service=definition.service,
        parameters=canonical,
        data_version_ids=versions,
        operation_key=build_operation_key(capability, canonical, versions),
    )
    return asyncio.run(tools.execute(operation, access))


def _shopper_access(*, principal_id: str = "shopper_read") -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="tenant_read",
            principal_id=principal_id,
            mode=ConversationMode.SHOPPER,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read"}),
    )


def _merchant_access() -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="tenant_read",
            principal_id="merchant_read",
            mode=ConversationMode.MERCHANT,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read", "merchant.read"}),
    )


def _seed_catalog_and_sandbox(session: Session) -> None:
    source = DatasetSource(
        id=1,
        dataset_id="v2-read-tools-fixture",
        dataset_version="2026-08-30",
        profile="test",
        source_url="https://example.test/synthetic-books",
        source_license="Synthetic test fixture",
        source_revision=1,
        raw_archive_sha256="a" * 64,
        snapshot_sha256="b" * 64,
        products_sha256="c" * 64,
        reviews_sha256="d" * 64,
        sampling_seed=1,
        product_count=3,
        review_count=0,
        retrieved_at=OBSERVED_AT,
    )
    session.add(source)
    session.flush()
    session.add_all(
        [
            Product(
                id=1,
                source_id=1,
                external_id="read-one",
                name="Historical book one",
                authors=["Synthetic author"],
                publisher="Synthetic publisher",
                category="Books",
                page_count=100,
                price=500_000,
                rating=4.8,
                sold_count=30,
                source_review_count=0,
                description="Active demo offer book.",
                platform="Tiki",
            ),
            Product(
                id=2,
                source_id=1,
                external_id="read-two",
                name="Historical book two",
                authors=["Synthetic author"],
                publisher="Synthetic publisher",
                category="Books",
                page_count=100,
                price=90_000,
                rating=4.7,
                sold_count=20,
                source_review_count=0,
                description="Historical price differs from demo price.",
                platform="Tiki",
            ),
            Product(
                id=3,
                source_id=1,
                external_id="read-no-offer",
                name="No offer book",
                authors=["Synthetic author"],
                publisher="Synthetic publisher",
                category="Books",
                page_count=100,
                price=70_000,
                rating=4.6,
                sold_count=10,
                source_review_count=0,
                description="Historical only.",
                platform="Tiki",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            V2Offer(
                id="offer_read_one",
                tenant_id="tenant_read",
                store_id="demo",
                product_id=1,
                demo_price_vnd=42_000,
                stock=7,
                version=3,
                is_active=True,
            ),
            V2Offer(
                id="offer_read_two",
                tenant_id="tenant_read",
                store_id="demo",
                product_id=2,
                demo_price_vnd=180_000,
                stock=10,
                version=1,
                is_active=True,
            ),
            V2Cart(
                id="cart_read",
                tenant_id="tenant_read",
                principal_id="shopper_read",
                store_id="demo",
                status="active",
                version=1,
            ),
        ]
    )
    session.flush()
    session.add(
        V2CartLine(
            id="line_read",
            cart_id="cart_read",
            tenant_id="tenant_read",
            principal_id="shopper_read",
            store_id="demo",
            offer_id="offer_read_one",
            quantity=2,
            offer_version=3,
        )
    )
