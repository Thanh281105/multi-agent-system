"""Real SQL regressions for catalog availability in isolated evaluation tenants."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import select

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_executor import EvaluationV3ObservationExecutor
from app.evaluation.v3_runner import SandboxFixtureAdapterV3
from app.models.product import Product
from app.models.v2 import V2Offer
from app.v2.actions import V2ActionService
from app.v2.registry import CatalogSearchInput, ProductResult
from app.v2.tools import V2ReadTools, load_catalog_snapshot
from tests.test_evaluation_v3_executor import _context_and_case
from tests.test_evaluation_v3_executor_postgres import (
    _postgres_store,
    _runtime,
    _SuccessfulHandler,
)


def _executor(store: Any, context: Any, case: Any) -> Any:
    runtime = _runtime(store, _SuccessfulHandler(), context)
    snapshot = load_catalog_snapshot(store.sessions)
    runtime.shared_services.action_service = V2ActionService(
        store.sessions, catalog_version_id=snapshot.version_id
    )
    return EvaluationV3ObservationExecutor(
        runtime=cast(Any, runtime), context=context, case=case
    )


@pytest.mark.asyncio
async def test_read_case_can_search_catalog_after_reset_without_foreign_offers() -> (
    None
):
    with _postgres_store("thanh_v2_p2_catalogseed_") as store:
        with store.sessions() as session, session.begin():
            phone = session.get(Product, 7)
            assert phone is not None
            phone.name, phone.category = "Điện thoại Samsung", "Điện thoại"
            phone.authors, phone.publisher, phone.page_count = [], None, None
            zero_price = session.get(Product, 8)
            assert zero_price is not None
            zero_price.price = 0
            session.add(
                V2Offer(
                    id="foreign_offer",
                    tenant_id="foreign",
                    store_id="demo",
                    product_id=1,
                    demo_price_vnd=900_000,
                    stock=3,
                    version=9,
                    is_active=False,
                )
            )
        context, case = _context_and_case(run_id="run_catalog_seed")
        executor = _executor(store, context, case)
        await executor.reset_initial_state(context=context, case=case)
        await executor.reset_initial_state(context=context, case=case)
        with store.sessions() as session:
            offers = list(
                session.scalars(
                    select(V2Offer).where(
                        V2Offer.tenant_id == context.namespace.tenant_id
                    )
                )
            )
            assert {offer.product_id for offer in offers} == set(range(1, 7))
            assert all(
                offer.demo_price_vnd == 100_000 + offer.product_id * 10_000
                for offer in offers
            )
            foreign = session.get(V2Offer, "foreign_offer")
            assert foreign is not None
            assert (
                foreign.demo_price_vnd,
                foreign.stock,
                foreign.version,
                foreign.is_active,
            ) == (900_000, 3, 9, False)
        snapshot = load_catalog_snapshot(store.sessions)
        tools = V2ReadTools(
            store.sessions,
            catalog_snapshot=snapshot,
            action_service=executor.runtime.shared_services.action_service,
        )
        output, evidence = tools._read(
            "product.catalog.search",
            CatalogSearchInput(query="Sách thử nghiệm 1"),
            executor._access(),
        )
        assert isinstance(output, ProductResult)
        assert [product.product_id for product in output.products] == [1]
        assert output.products[0].price_vnd == 110_000
        assert any(
            reference.source_id == "catalog_product_1"
            for reference in evidence.references
        )
        assert store.ledger.account_summary("p7-executor-account").known_nano_usd == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["shopper", "merchant"])
async def test_explicit_action_fixture_keeps_exact_offer_roster(role: str) -> None:
    with _postgres_store("thanh_v2_p2_catalogfix_") as store:
        payload = (
            {
                "cart": {
                    "cart_id": "cart_fixture",
                    "version": 1,
                    "lines": [
                        {"product_id": 1, "unit_price_vnd": 90_000, "quantity": 2}
                    ],
                }
            }
            if role == "shopper"
            else {
                "merchant": {
                    "offers": [
                        {
                            "offer_id": "fixture_offer",
                            "product_id": 1,
                            "price_vnd": 90_000,
                            "available_quantity": 2,
                            "version": 1,
                        }
                    ]
                }
            }
        )
        payload.update(fixture_id="catalog_action_fixture", reset_revision=1)
        _, original = _context_and_case()
        case = original.model_copy(
            update={
                "principal_role": role,
                "category": "shopping_merchant",
                "sandbox_fixture": SandboxFixtureAdapterV3(
                    fixture_id="catalog_action_fixture",
                    fixture_sha256=canonical_sha256(payload),
                    reset_revision=1,
                    payload=payload,
                ),
            }
        )
        context, case = _context_and_case(case, run_id="run_catalog_fixture_" + role)
        executor = _executor(store, context, case)
        await executor.reset_initial_state(context=context, case=case)
        with store.sessions() as session:
            offers = list(
                session.scalars(
                    select(V2Offer).where(
                        V2Offer.tenant_id == context.namespace.tenant_id
                    )
                )
            )
            assert len(offers) == 1
            assert (
                offers[0].product_id,
                offers[0].demo_price_vnd,
                offers[0].stock,
            ) == (1, 90_000, 2)
