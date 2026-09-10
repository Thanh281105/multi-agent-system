"""Real PostgreSQL transaction and locking tests for v2 sandbox actions."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier, Lock

import pytest
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext
from app.db.migrate import upgrade_database
from app.models.product import Product
from app.models.v2 import (
    V2ActionAudit,
    V2ActionIdempotency,
    V2Cart,
    V2Offer,
    V2Order,
    V2OrderItem,
    V2Proposal,
)
from app.v2.actions import (
    ActionIdempotencyConflictError,
    ActionStateConflictError,
    V2ActionService,
)
from app.v2.authorization import AuthorizationDeniedError, ResourceNotFoundError
from app.v2.contracts import (
    ActionConfirmRequest,
    ActionRejectRequest,
    ActionStatus,
    ConversationMode,
)
from app.v2.registry import (
    CartChangeInput,
    CartReadInput,
    CheckoutInput,
    MerchantOfferProposalInput,
    MerchantReadInput,
)
from tests.v2_postgres_support import disposable_postgres_database


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    with disposable_postgres_database("thanh_v2_p2_actions_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def postgres_sessions(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


@pytest.fixture
def actions(postgres_sessions: sessionmaker[Session]) -> V2ActionService:
    return V2ActionService(
        postgres_sessions,
        catalog_version_id="catalog_actions_v1",
    )


@dataclass(frozen=True, slots=True)
class ShopperFixture:
    suffix: str
    authorization: AuthorizationContext
    conversation_id: str
    turn_id: str
    cart_id: str
    offer_id: str
    product_id: int


@dataclass(frozen=True, slots=True)
class MerchantFixture:
    suffix: str
    authorization: AuthorizationContext
    conversation_id: str
    turn_id: str
    offer_id: str
    product_id: int


def test_cart_inventory_reads_and_cart_set_remove_are_owner_scoped(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "cartcrud", quantity=1)
    merchant = _merchant_auth(fixture.suffix)
    inventory = actions.read_inventory(
        merchant, MerchantReadInput(product_ids=(fixture.product_id,))
    )
    assert [(item.offer_id, item.available_quantity) for item in inventory.offers] == [
        (fixture.offer_id, 10)
    ]

    changed = actions.set_cart_item(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        request=CartChangeInput(
            cart_id=fixture.cart_id,
            product_id=fixture.product_id,
            quantity=3,
            expected_version=1,
        ),
    )
    assert changed.status == ActionStatus.EXECUTED
    assert changed.resource_version == 2
    cart = actions.read_cart(
        fixture.authorization, CartReadInput(), cart_id=fixture.cart_id
    )
    assert cart.version == 2 and cart.items[0].quantity == 3

    removed = actions.set_cart_item(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        request=CartChangeInput(
            cart_id=fixture.cart_id,
            product_id=fixture.product_id,
            quantity=0,
            expected_version=2,
        ),
    )
    assert removed.resource_version == 3
    assert (
        actions.read_cart(
            fixture.authorization, CartReadInput(), cart_id=fixture.cart_id
        ).items
        == ()
    )
    with postgres_sessions() as session:
        audits = session.scalar(
            select(func.count())
            .select_from(V2ActionAudit)
            .where(V2ActionAudit.tenant_id == f"tenant_{fixture.suffix}")
        )
        assert audits == 2


def test_postgres_same_key_exact_replay_and_lost_response_mutate_once(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture, action_id = _checkout_proposal(actions, postgres_sessions, "exactreplay")
    request = ActionConfirmRequest(proposal_version=1)
    first = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=request,
        idempotency_key="checkout-exact-1",
    )
    replay = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=request,
        idempotency_key="checkout-exact-1",
    )
    assert replay.model_dump(mode="json") == first.model_dump(mode="json")
    _assert_checkout_effect(postgres_sessions, fixture, expected_stock=8)
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionIdempotency)
                .where(V2ActionIdempotency.proposal_id == action_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionAudit)
                .where(V2ActionAudit.proposal_id == action_id)
            )
            == 1
        )


def test_postgres_same_key_different_payload_conflicts(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture, action_id = _checkout_proposal(actions, postgres_sessions, "keyconflict")
    actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key="checkout-conflict-1",
    )
    with pytest.raises(ActionIdempotencyConflictError):
        actions.confirm_action(
            fixture.authorization,
            action_id=action_id,
            request=ActionConfirmRequest(proposal_version=2),
            idempotency_key="checkout-conflict-1",
        )
    _assert_checkout_effect(postgres_sessions, fixture, expected_stock=8)


@pytest.mark.parametrize("same_key", [True, False])
def test_postgres_concurrent_confirmation_has_one_checkout_effect(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
    same_key: bool,
) -> None:
    suffix = "concurrentsame" if same_key else "concurrentdifferent"
    fixture, action_id = _checkout_proposal(actions, postgres_sessions, suffix)
    barrier = Barrier(2)

    def confirm(position: int) -> dict[str, object]:
        barrier.wait(timeout=10)
        result = actions.confirm_action(
            fixture.authorization,
            action_id=action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key=(
                "concurrent-shared-key" if same_key else f"concurrent-key-{position}"
            ),
        )
        return result.model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(confirm, (1, 2)))
    assert results[0] == results[1]
    _assert_checkout_effect(postgres_sessions, fixture, expected_stock=8)
    with postgres_sessions() as session:
        expected_rows = 1 if same_key else 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionIdempotency)
                .where(V2ActionIdempotency.proposal_id == action_id)
            )
            == expected_rows
        )


def test_postgres_failure_before_commit_rolls_back_everything(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture, action_id = _checkout_proposal(actions, postgres_sessions, "rollback")
    failing = _FailBeforeCommitService(
        postgres_sessions, catalog_version_id="catalog_actions_v1"
    )
    with pytest.raises(RuntimeError, match="synthetic pre-commit failure"):
        failing.confirm_action(
            fixture.authorization,
            action_id=action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key="rollback-confirm-key",
        )
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Order)
                .where(V2Order.cart_id == fixture.cart_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionIdempotency)
                .where(V2ActionIdempotency.idempotency_key == "rollback-confirm-key")
            )
            == 0
        )
        assert session.get(V2Offer, fixture.offer_id).stock == 10  # type: ignore[union-attr]
        assert session.get(V2Cart, fixture.cart_id).status == "active"  # type: ignore[union-attr]

    result = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key="rollback-confirm-key",
    )
    assert result.status == ActionStatus.EXECUTED
    _assert_checkout_effect(postgres_sessions, fixture, expected_stock=8)


@pytest.mark.parametrize("mutation", ["price", "stock", "cart"])
def test_postgres_checkout_detects_stale_price_stock_or_cart(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
    mutation: str,
) -> None:
    fixture, action_id = _checkout_proposal(
        actions, postgres_sessions, f"stale{mutation}"
    )
    with postgres_sessions.begin() as session:
        if mutation == "cart":
            cart = session.get(V2Cart, fixture.cart_id)
            assert cart is not None
            cart.version += 1
        else:
            offer = session.get(V2Offer, fixture.offer_id)
            assert offer is not None
            if mutation == "price":
                offer.demo_price_vnd += 1
            else:
                offer.stock -= 1
            offer.version += 1
    result = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key=f"stale-{mutation}-key",
    )
    replay = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key=f"stale-{mutation}-key",
    )
    assert result.status == ActionStatus.CONFLICTED
    assert replay.model_dump(mode="json") == result.model_dump(mode="json")
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Order)
                .where(V2Order.cart_id == fixture.cart_id)
            )
            == 0
        )
        assert session.get(V2Proposal, action_id).status == "conflicted"  # type: ignore[union-attr]


def test_postgres_expiry_and_insufficient_stock_are_terminal_and_replayable(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    expired_fixture, expired_id = _checkout_proposal(
        actions, postgres_sessions, "expired"
    )
    with postgres_sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_proposals SET expires_at = now() - interval '1 second' "
                "WHERE id = :proposal_id"
            ),
            {"proposal_id": expired_id},
        )
    expired = actions.confirm_action(
        expired_fixture.authorization,
        action_id=expired_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key="expired-confirm-key",
    )
    assert expired.status == ActionStatus.EXPIRED

    stock_fixture, stock_id = _checkout_proposal(
        actions, postgres_sessions, "insufficient"
    )
    with postgres_sessions.begin() as session:
        offer = session.get(V2Offer, stock_fixture.offer_id)
        assert offer is not None
        offer.stock = 1
        offer.version += 1
    insufficient = actions.confirm_action(
        stock_fixture.authorization,
        action_id=stock_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key="insufficient-confirm-key",
    )
    assert insufficient.status == ActionStatus.CONFLICTED
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Order)
                .where(
                    V2Order.cart_id.in_(
                        (expired_fixture.cart_id, stock_fixture.cart_id)
                    )
                )
            )
            == 0
        )


def test_postgres_confirm_reject_race_has_one_terminal_winner(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture, action_id = _checkout_proposal(actions, postgres_sessions, "decisionrace")
    barrier = Barrier(2)

    def confirm() -> str:
        barrier.wait(timeout=10)
        try:
            return actions.confirm_action(
                fixture.authorization,
                action_id=action_id,
                request=ActionConfirmRequest(proposal_version=1),
                idempotency_key="decision-race-key",
            ).status.value
        except ActionStateConflictError:
            return "lost"

    def reject() -> str:
        barrier.wait(timeout=10)
        try:
            return actions.reject_action(
                fixture.authorization,
                action_id=action_id,
                request=ActionRejectRequest(proposal_version=1, reason="Không mua nữa"),
            ).status.value
        except ActionStateConflictError:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirm_future = executor.submit(confirm)
        reject_future = executor.submit(reject)
        outcomes = {confirm_future.result(), reject_future.result()}
    assert "lost" in outcomes
    assert outcomes & {"executed", "rejected"}
    with postgres_sessions() as session:
        proposal = session.get(V2Proposal, action_id)
        assert proposal is not None
        order_count = session.scalar(
            select(func.count())
            .select_from(V2Order)
            .where(V2Order.cart_id == fixture.cart_id)
        )
        assert order_count == (1 if proposal.status == "executed" else 0)
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionAudit)
                .where(V2ActionAudit.proposal_id == action_id)
            )
            == 1
        )


def test_postgres_owner_mode_and_cancelled_turn_are_enforced(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    merchant, action_id = _merchant_proposal(
        actions, postgres_sessions, "authmerchant", price=110_000
    )
    foreign = AuthorizationContext(
        tenant_id="tenant_foreign",
        principal_id=merchant.authorization.principal_id,
        scopes=merchant.authorization.scopes,
    )
    with pytest.raises(ResourceNotFoundError):
        actions.confirm_action(
            foreign,
            action_id=action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key="foreign-confirm-key",
        )
    shopper_scopes = AuthorizationContext(
        tenant_id=merchant.authorization.tenant_id,
        principal_id=merchant.authorization.principal_id,
        scopes=frozenset({"ecommerce.read", "ecommerce.write"}),
    )
    with pytest.raises(AuthorizationDeniedError):
        actions.confirm_action(
            shopper_scopes,
            action_id=action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key="shopper-confirm-key",
        )

    cancelled, cancelled_id = _checkout_proposal(
        actions, postgres_sessions, "cancelled"
    )
    with postgres_sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_turns SET execution_state = 'cancelled', "
                "dialogue_outcome = NULL, result = NULL, completed_at = now() "
                "WHERE id = :turn_id"
            ),
            {"turn_id": cancelled.turn_id},
        )
    with pytest.raises(ActionStateConflictError):
        actions.confirm_action(
            cancelled.authorization,
            action_id=cancelled_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key="cancelled-confirm-key",
        )
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Order)
                .where(V2Order.cart_id == cancelled.cart_id)
            )
            == 0
        )


@pytest.mark.parametrize("change", ["price", "stock"])
def test_postgres_merchant_offer_executes_once_and_versions_only_offer(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
    change: str,
) -> None:
    fixture, action_id = _merchant_proposal(
        actions,
        postgres_sessions,
        f"merchant{change}",
        price=110_000 if change == "price" else None,
        quantity_delta=-3 if change == "stock" else None,
    )
    result = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key=f"merchant-{change}-key",
    )
    replay = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key=f"merchant-{change}-key",
    )
    assert result.status == ActionStatus.EXECUTED
    assert replay.model_dump(mode="json") == result.model_dump(mode="json")
    with postgres_sessions() as session:
        offer = session.get(V2Offer, fixture.offer_id)
        product = session.get(Product, fixture.product_id)
        assert offer is not None and product is not None
        assert offer.version == 2
        assert offer.demo_price_vnd == (110_000 if change == "price" else 100_000)
        assert offer.stock == (7 if change == "stock" else 10)
        assert product.price == 100_000


def test_postgres_action_result_read_and_immutable_checkout_rows(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture, action_id = _checkout_proposal(actions, postgres_sessions, "readresult")
    result = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key="read-result-key",
    )
    stored = actions.read_action_result(fixture.authorization, action_id=action_id)
    assert stored.card.action_id == stored.card.proposal_id == action_id
    assert stored.result is not None
    assert stored.result["response"] == result.model_dump(mode="json")
    with pytest.raises(IntegrityError, match="immutable"):
        with postgres_sessions.begin() as session:
            session.execute(
                text("UPDATE v2_orders SET total_vnd = 1 WHERE id = :order_id"),
                {"order_id": result.resource_id},
            )


class _FailBeforeCommitService(V2ActionService):
    def __init__(
        self, session_factory: sessionmaker[Session], *, catalog_version_id: str
    ) -> None:
        super().__init__(session_factory, catalog_version_id=catalog_version_id)
        self._failed = False
        self._failure_lock = Lock()

    def _before_commit(self) -> None:
        with self._failure_lock:
            if not self._failed:
                self._failed = True
                raise RuntimeError("synthetic pre-commit failure")


def _checkout_proposal(
    actions: V2ActionService,
    sessions: sessionmaker[Session],
    suffix: str,
) -> tuple[ShopperFixture, str]:
    fixture = _seed_shopper(sessions, suffix, quantity=2)
    preview = actions.preview_checkout(
        fixture.authorization,
        CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
    )
    assert preview.can_checkout
    proposal = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
    )
    assert proposal.action.action_id == proposal.action.proposal_id
    replay = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
    )
    assert replay.model_dump(mode="json") == proposal.model_dump(mode="json")
    with sessions() as session:
        stored = session.get(V2Proposal, proposal.action.proposal_id)
        assert stored is not None
        assert (stored.expires_at - stored.created_at).total_seconds() == 600
    _finish_turn(sessions, fixture.turn_id)
    return fixture, proposal.action.action_id


def _merchant_proposal(
    actions: V2ActionService,
    sessions: sessionmaker[Session],
    suffix: str,
    *,
    price: int | None = None,
    quantity_delta: int | None = None,
) -> tuple[MerchantFixture, str]:
    fixture = _seed_merchant(sessions, suffix)
    proposal = actions.propose_offer_change(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        request=MerchantOfferProposalInput(
            offer_id=fixture.offer_id,
            expected_version=1,
            new_price_vnd=price,
            quantity_delta=quantity_delta,
        ),
    )
    _finish_turn(sessions, fixture.turn_id)
    return fixture, proposal.action.action_id


def _seed_shopper(
    sessions: sessionmaker[Session], suffix: str, *, quantity: int
) -> ShopperFixture:
    product_id = _product_id(suffix)
    tenant_id = f"tenant_{suffix}"
    principal_id = f"principal_{suffix}"
    conversation_id = f"conversation_{suffix}"
    turn_id = f"turn_{suffix}"
    cart_id = f"cart_{suffix}"
    offer_id = f"offer_{suffix}"
    with sessions.begin() as session:
        _insert_product_offer(session, product_id, offer_id, tenant_id)
        _insert_context(
            session,
            conversation_id,
            turn_id,
            tenant_id,
            principal_id,
            ConversationMode.SHOPPER,
        )
        session.execute(
            text(
                "INSERT INTO v2_carts "
                "(id, tenant_id, principal_id, store_id, status, version) "
                "VALUES (:id, :tenant, :principal, 'demo', 'active', 1)"
            ),
            {"id": cart_id, "tenant": tenant_id, "principal": principal_id},
        )
        if quantity:
            session.execute(
                text(
                    "INSERT INTO v2_cart_lines "
                    "(id, cart_id, tenant_id, principal_id, store_id, offer_id, "
                    "quantity, offer_version) VALUES "
                    "(:id, :cart, :tenant, :principal, 'demo', :offer, :quantity, 1)"
                ),
                {
                    "id": f"line_{suffix}",
                    "cart": cart_id,
                    "tenant": tenant_id,
                    "principal": principal_id,
                    "offer": offer_id,
                    "quantity": quantity,
                },
            )
    return ShopperFixture(
        suffix=suffix,
        authorization=_shopper_auth(suffix),
        conversation_id=conversation_id,
        turn_id=turn_id,
        cart_id=cart_id,
        offer_id=offer_id,
        product_id=product_id,
    )


def _seed_merchant(sessions: sessionmaker[Session], suffix: str) -> MerchantFixture:
    product_id = _product_id(suffix)
    tenant_id = f"tenant_{suffix}"
    principal_id = f"principal_{suffix}"
    conversation_id = f"conversation_{suffix}"
    turn_id = f"turn_{suffix}"
    offer_id = f"offer_{suffix}"
    with sessions.begin() as session:
        _insert_product_offer(session, product_id, offer_id, tenant_id)
        _insert_context(
            session,
            conversation_id,
            turn_id,
            tenant_id,
            principal_id,
            ConversationMode.MERCHANT,
        )
    return MerchantFixture(
        suffix=suffix,
        authorization=_merchant_auth(suffix),
        conversation_id=conversation_id,
        turn_id=turn_id,
        offer_id=offer_id,
        product_id=product_id,
    )


def _insert_product_offer(
    session: Session, product_id: int, offer_id: str, tenant_id: str
) -> None:
    session.execute(
        text(
            "INSERT INTO products "
            "(id, name, category, price, description, platform) VALUES "
            "(:id, :name, 'Books', 100000, 'Synthetic action fixture', 'Tiki')"
        ),
        {"id": product_id, "name": f"Synthetic {offer_id}"},
    )
    session.execute(
        text(
            "INSERT INTO v2_offers "
            "(id, tenant_id, store_id, product_id, demo_price_vnd, stock, version) "
            "VALUES (:id, :tenant, 'demo', :product, 100000, 10, 1)"
        ),
        {"id": offer_id, "tenant": tenant_id, "product": product_id},
    )


def _insert_context(
    session: Session,
    conversation_id: str,
    turn_id: str,
    tenant_id: str,
    principal_id: str,
    mode: ConversationMode,
) -> None:
    session.execute(
        text(
            "INSERT INTO v2_conversations "
            "(id, tenant_id, principal_id, mode, store_id) VALUES "
            "(:id, :tenant, :principal, :mode, 'demo')"
        ),
        {
            "id": conversation_id,
            "tenant": tenant_id,
            "principal": principal_id,
            "mode": mode.value,
        },
    )
    session.execute(
        text(
            "INSERT INTO v2_turns "
            "(id, conversation_id, tenant_id, principal_id, mode, store_id, "
            "client_turn_id, request_payload_hash, request_payload, execution_state) "
            "VALUES (:id, :conversation, :tenant, :principal, :mode, 'demo', "
            ":client, :hash, '{}', 'running')"
        ),
        {
            "id": turn_id,
            "conversation": conversation_id,
            "tenant": tenant_id,
            "principal": principal_id,
            "mode": mode.value,
            "client": f"client-{turn_id}",
            "hash": "a" * 64,
        },
    )


def _finish_turn(sessions: sessionmaker[Session], turn_id: str) -> None:
    with sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_turns SET execution_state = 'completed', "
                "dialogue_outcome = 'awaiting_confirmation', result = '{}', "
                "completed_at = now() WHERE id = :turn_id"
            ),
            {"turn_id": turn_id},
        )


def _assert_checkout_effect(
    sessions: sessionmaker[Session],
    fixture: ShopperFixture,
    *,
    expected_stock: int,
) -> None:
    with sessions() as session:
        cart = session.get(V2Cart, fixture.cart_id)
        offer = session.get(V2Offer, fixture.offer_id)
        assert cart is not None and offer is not None
        assert cart.status == "checked_out" and cart.version == 2
        assert offer.stock == expected_stock and offer.version == 2
        order = session.scalar(select(V2Order).where(V2Order.cart_id == cart.id))
        assert order is not None and order.cart_version == 1
        items = tuple(
            session.scalars(select(V2OrderItem).where(V2OrderItem.order_id == order.id))
        )
        assert len(items) == 1
        assert items[0].quantity == 2 and items[0].unit_price_vnd == 100_000


def _shopper_auth(suffix: str) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id=f"tenant_{suffix}",
        principal_id=f"principal_{suffix}",
        scopes=frozenset({"ecommerce.read", "ecommerce.write"}),
    )


def _merchant_auth(suffix: str) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id=f"tenant_{suffix}",
        principal_id=f"principal_{suffix}",
        scopes=frozenset({"ecommerce.read", "merchant.read", "merchant.write"}),
    )


def _product_id(suffix: str) -> int:
    return 1_000_000 + int.from_bytes(suffix.encode("utf-8"), "little") % 1_000_000_000
