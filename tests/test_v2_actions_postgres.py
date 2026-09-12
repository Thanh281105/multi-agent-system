"""Real PostgreSQL transaction and locking tests for v2 sandbox actions."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier, Event, Lock
from time import sleep

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
    V2Conversation,
    V2Offer,
    V2Order,
    V2OrderItem,
    V2Proposal,
    V2Turn,
)
from app.v2.actions import (
    ActionConflictError,
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
from app.v2.history import V2HistoryService
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
    lease_owner: str
    cart_id: str
    offer_id: str
    product_id: int


@dataclass(frozen=True, slots=True)
class MerchantFixture:
    suffix: str
    authorization: AuthorizationContext
    conversation_id: str
    turn_id: str
    lease_owner: str
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


def test_postgres_stale_direct_cart_change_is_readable_after_restart(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "staledirectcart", quantity=1)
    with postgres_sessions.begin() as session:
        cart = session.get(V2Cart, fixture.cart_id)
        assert cart is not None
        cart.version += 1

    with postgres_sessions() as session:
        audit_count_before = session.scalar(
            select(func.count())
            .select_from(V2ActionAudit)
            .where(V2ActionAudit.tenant_id == f"tenant_{fixture.suffix}")
        )

    with pytest.raises(ActionConflictError, match="cart_version_conflict"):
        actions.set_cart_item(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            request=CartChangeInput(
                cart_id=fixture.cart_id,
                product_id=fixture.product_id,
                quantity=2,
                expected_version=1,
            ),
        )

    restarted_actions = V2ActionService(
        postgres_sessions,
        catalog_version_id="catalog_actions_v1",
    )
    cart_result = restarted_actions.read_cart(
        fixture.authorization,
        CartReadInput(),
        cart_id=fixture.cart_id,
    )
    assert cart_result.version == 2
    assert cart_result.total_price_vnd == 100_000
    assert [(item.product_id, item.quantity) for item in cart_result.items] == [
        (fixture.product_id, 1)
    ]
    with postgres_sessions() as session:
        audit_count_after = session.scalar(
            select(func.count())
            .select_from(V2ActionAudit)
            .where(V2ActionAudit.tenant_id == f"tenant_{fixture.suffix}")
        )
    assert audit_count_after == audit_count_before == 0


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
    restarted_actions = V2ActionService(
        postgres_sessions,
        catalog_version_id="catalog_actions_v1",
    )
    replay = restarted_actions.confirm_action(
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


def test_postgres_same_scoped_key_cannot_confirm_two_proposals(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_merchant(postgres_sessions, "crossproposalkey")
    second_product_id = _product_id("crossproposalkeysecond")
    second_offer_id = "offer_crossproposalkey_second"
    second_turn_id = "turn_crossproposalkey_second"
    second_lease_owner = f"worker_{second_turn_id}"
    with postgres_sessions.begin() as session:
        _insert_product_offer(
            session,
            second_product_id,
            second_offer_id,
            fixture.authorization.tenant_id,
        )
        _insert_running_turn(
            session,
            fixture.conversation_id,
            second_turn_id,
            fixture.authorization.tenant_id,
            fixture.authorization.principal_id,
            ConversationMode.MERCHANT,
        )
    first = actions.propose_offer_change(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=second_turn_id,
        lease_owner=second_lease_owner,
        request=MerchantOfferProposalInput(
            offer_id=fixture.offer_id,
            expected_version=1,
            new_price_vnd=110_000,
        ),
    )
    second = actions.propose_offer_change(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=MerchantOfferProposalInput(
            offer_id=second_offer_id,
            expected_version=1,
            new_price_vnd=120_000,
        ),
    )
    _finish_turn(postgres_sessions, fixture.turn_id)
    _finish_turn(postgres_sessions, second_turn_id)
    key = "same-scope-two-proposals"
    executed = actions.confirm_action(
        fixture.authorization,
        action_id=first.action.action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key=key,
    )
    assert executed.status == ActionStatus.EXECUTED
    with pytest.raises(ActionIdempotencyConflictError):
        actions.confirm_action(
            fixture.authorization,
            action_id=second.action.action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key=key,
        )
    with postgres_sessions() as session:
        first_offer = session.get(V2Offer, fixture.offer_id)
        second_offer = session.get(V2Offer, second_offer_id)
        assert first_offer is not None and second_offer is not None
        assert (first_offer.demo_price_vnd, first_offer.version) == (110_000, 2)
        assert (second_offer.demo_price_vnd, second_offer.version) == (100_000, 1)
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionAudit)
                .where(V2ActionAudit.event_type == "merchant.offer.executed")
                .where(
                    V2ActionAudit.proposal_id.in_(
                        (first.action.action_id, second.action.action_id)
                    )
                )
            )
            == 1
        )


def test_checkout_proposal_replays_identical_card_after_live_state_drift(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "checkoutreplaydrift", quantity=2)
    request = CheckoutInput(cart_id=fixture.cart_id, expected_version=1)
    first = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=request,
    )
    with postgres_sessions.begin() as session:
        cart = session.get(V2Cart, fixture.cart_id)
        offer = session.get(V2Offer, fixture.offer_id)
        assert cart is not None and offer is not None
        cart.version = 2
        offer.demo_price_vnd = 999_000
        offer.version = 2

    replay = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=request,
    )
    assert replay == first
    assert replay.action.target.expected_resource_version == 1


def test_merchant_proposal_replays_exact_target_after_offer_drift(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_merchant(postgres_sessions, "merchantreplaydrift")
    request = MerchantOfferProposalInput(
        offer_id=fixture.offer_id,
        expected_version=1,
        new_price_vnd=230_000,
    )
    first = actions.propose_offer_change(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=request,
    )
    with postgres_sessions.begin() as session:
        offer = session.get(V2Offer, fixture.offer_id)
        assert offer is not None
        offer.demo_price_vnd = 310_000
        offer.version = 2

    replay = actions.propose_offer_change(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=request,
    )
    assert replay == first
    assert replay.action.target.resource_id == fixture.offer_id
    assert replay.action.target.expected_resource_version == 1
    assert replay.action.changes[0].after_integer == 230_000


def test_proposal_creation_rejects_mismatched_and_expired_turn_leases(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "proposallease", quantity=1)
    request = CheckoutInput(cart_id=fixture.cart_id, expected_version=1)
    with pytest.raises(ActionStateConflictError, match="turn_lease_mismatch"):
        actions.propose_checkout(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            lease_owner="worker_wrong_owner",
            request=request,
        )
    with postgres_sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_turns SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = :turn_id"
            ),
            {"turn_id": fixture.turn_id},
        )
    with pytest.raises(ActionStateConflictError, match="turn_lease_expired"):
        actions.propose_checkout(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            lease_owner=fixture.lease_owner,
            request=request,
        )


def test_proposal_write_rechecks_lease_after_context_lock(
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "proposalwritefence", quantity=1)
    actions = _ExpireBeforeProposalWriteService(
        postgres_sessions,
        catalog_version_id="catalog_actions_v1",
    )
    with pytest.raises(ActionStateConflictError, match="turn_lease_expired"):
        actions.propose_checkout(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            lease_owner=fixture.lease_owner,
            request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
        )
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Proposal)
                .where(V2Proposal.turn_id == fixture.turn_id)
            )
            == 0
        )


def test_recovery_expires_stale_proposal_before_interrupting_turn(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "recoveryexpiredproposal", quantity=1)
    proposed = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
    )
    with postgres_sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_turns SET lease_expires_at = "
                "clock_timestamp() - interval '1 second' WHERE id = :turn_id"
            ),
            {"turn_id": fixture.turn_id},
        )
        session.execute(
            text(
                "UPDATE v2_proposals SET expires_at = "
                "clock_timestamp() - interval '1 second' WHERE id = :proposal_id"
            ),
            {"proposal_id": proposed.action.action_id},
        )

    recovered = actions.recover_expired_turn_proposal(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        mode=ConversationMode.SHOPPER,
    )
    replay = actions.recover_expired_turn_proposal(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        mode=ConversationMode.SHOPPER,
    )
    assert recovered is True and replay is True
    with postgres_sessions() as session:
        turn = session.get(V2Turn, fixture.turn_id)
        proposal = session.get(V2Proposal, proposed.action.action_id)
        assert turn is not None and proposal is not None
        assert turn.execution_state == "interrupted"
        assert turn.lease_owner is None and turn.lease_expires_at is None
        assert proposal.status == ActionStatus.EXPIRED.value
        assert proposal.resolved_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionAudit)
                .where(
                    V2ActionAudit.proposal_id == proposal.id,
                    V2ActionAudit.event_type == "action.expired",
                )
            )
            == 1
        )


@pytest.mark.parametrize("duplicate", [False, True])
def test_recovery_fails_all_corrupt_pending_proposals(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
    duplicate: bool,
) -> None:
    fixture = _seed_shopper(
        postgres_sessions, f"recoverycorrupt{str(duplicate).lower()}", quantity=1
    )
    proposed = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
    )
    with postgres_sessions.begin() as session:
        proposal = session.get(V2Proposal, proposed.action.action_id)
        turn = session.get(V2Turn, fixture.turn_id)
        assert proposal is not None and turn is not None
        proposal.after_payload = {**proposal.after_payload, "total_vnd": 1}
        if duplicate:
            session.add(
                V2Proposal(
                    id=f"proposal_duplicate_recovery_{fixture.suffix}",
                    tenant_id=proposal.tenant_id,
                    principal_id=proposal.principal_id,
                    mode=proposal.mode,
                    store_id=proposal.store_id,
                    conversation_id=proposal.conversation_id,
                    turn_id=proposal.turn_id,
                    action_type=proposal.action_type,
                    target_type=proposal.target_type,
                    target_id=proposal.target_id,
                    target_version=proposal.target_version,
                    before_payload=proposal.before_payload,
                    after_payload=proposal.after_payload,
                    proposal_version=proposal.proposal_version,
                    status=ActionStatus.PROPOSED.value,
                    expires_at=proposal.expires_at,
                )
            )
        session.execute(
            text(
                "UPDATE v2_turns SET lease_expires_at = "
                "clock_timestamp() - interval '1 second' WHERE id = :turn_id"
            ),
            {"turn_id": fixture.turn_id},
        )

    assert actions.recover_expired_turn_proposal(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        mode=ConversationMode.SHOPPER,
    )
    with postgres_sessions() as session:
        turn = session.get(V2Turn, fixture.turn_id)
        proposals = tuple(
            session.scalars(
                select(V2Proposal)
                .where(V2Proposal.turn_id == fixture.turn_id)
                .order_by(V2Proposal.id)
            )
        )
        assert turn is not None
        assert turn.execution_state == "interrupted"
        assert turn.dialogue_outcome is None and turn.result is None
        assert len(proposals) == (2 if duplicate else 1)
        assert all(item.status == ActionStatus.FAILED.value for item in proposals)
        assert all(item.resolved_at is not None for item in proposals)
        assert all(
            item.result is not None
            and item.result["response"]["status"] == ActionStatus.FAILED.value
            for item in proposals
        )
        assert session.scalar(
            select(func.count())
            .select_from(V2ActionAudit)
            .where(V2ActionAudit.proposal_id.in_(tuple(item.id for item in proposals)))
        ) == len(proposals)


def test_recovery_racing_proposal_commit_observes_committed_card(
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "recoverycommit", quantity=1)
    actions = _BlockProposalCommitService(
        postgres_sessions,
        catalog_version_id="catalog_actions_v1",
    )
    with postgres_sessions.begin() as session:
        turn = session.get(V2Turn, fixture.turn_id)
        assert turn is not None
        db_now = session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        turn.lease_expires_at = db_now + timedelta(seconds=4)
        turn.runtime_metadata = {
            "schema_version": 1,
            "data_versions": {
                "catalog_version_id": "catalog_actions_v1",
                "corpus_version_id": "corpus_actions_v1",
                "index_manifest_id": "index_actions_v1",
            },
            "knowledge_retrievals": 0,
            "draft_repairs": 0,
        }

    def propose() -> object:
        return actions.propose_checkout(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            lease_owner=fixture.lease_owner,
            request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
        )

    recovery_started = Event()

    def recover() -> bool:
        recovery_started.set()
        return actions.recover_expired_turn_proposal(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            mode=ConversationMode.SHOPPER,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        proposal_future = executor.submit(propose)
        assert actions.commit_entered.wait(timeout=10)
        recovery_future = executor.submit(recover)
        assert recovery_started.wait(timeout=10)
        sleep(4.2)
        assert not recovery_future.done()
        actions.allow_commit.set()
        proposal = proposal_future.result(timeout=10)
        assert recovery_future.result(timeout=10) is True

    with postgres_sessions() as session:
        turn = session.get(V2Turn, fixture.turn_id)
        assert turn is not None and turn.result is not None
        assert turn.execution_state == "completed"
        assert turn.dialogue_outcome == "awaiting_confirmation"
        assert turn.result["turn_result"]["action_cards"] == [
            proposal.action.model_dump(mode="json")
        ]


def test_concurrent_different_semantics_leave_one_turn_proposal(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_merchant(postgres_sessions, "proposalconcurrent")
    barrier = Barrier(2)

    def propose(price: int) -> tuple[str, str]:
        barrier.wait(timeout=10)
        try:
            result = actions.propose_offer_change(
                fixture.authorization,
                conversation_id=fixture.conversation_id,
                turn_id=fixture.turn_id,
                lease_owner=fixture.lease_owner,
                request=MerchantOfferProposalInput(
                    offer_id=fixture.offer_id,
                    expected_version=1,
                    new_price_vnd=price,
                ),
            )
        except ActionStateConflictError as exc:
            return "conflict", str(exc)
        return "created", result.action.action_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(propose, (150_000, 160_000)))
    assert sorted(item[0] for item in outcomes) == ["conflict", "created"]
    assert next(value for status, value in outcomes if status == "conflict") == (
        "turn_proposal_conflict"
    )
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Proposal)
                .where(V2Proposal.turn_id == fixture.turn_id)
            )
            == 1
        )


def test_concurrent_same_semantics_replay_one_turn_proposal(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_merchant(postgres_sessions, "proposalsamerequest")
    barrier = Barrier(2)
    request = MerchantOfferProposalInput(
        offer_id=fixture.offer_id,
        expected_version=1,
        new_price_vnd=175_000,
    )

    def propose() -> dict[str, object]:
        barrier.wait(timeout=10)
        return actions.propose_offer_change(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            lease_owner=fixture.lease_owner,
            request=request,
        ).model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(propose) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    with postgres_sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Proposal)
                .where(V2Proposal.turn_id == fixture.turn_id)
            )
            == 1
        )


def test_read_turn_proposal_hides_cross_owner(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_merchant(postgres_sessions, "proposalreadscope")
    request = MerchantOfferProposalInput(
        offer_id=fixture.offer_id,
        expected_version=1,
        quantity_delta=-2,
    )
    proposed = actions.propose_offer_change(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=request,
    )
    stored = actions.read_turn_proposal(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        mode=ConversationMode.MERCHANT,
    )
    assert stored is not None and stored.card == proposed.action
    foreign_authorizations = (
        AuthorizationContext(
            tenant_id=fixture.authorization.tenant_id,
            principal_id="principal_foreign_proposal_reader",
            scopes=fixture.authorization.scopes,
        ),
        AuthorizationContext(
            tenant_id="tenant_foreign_proposal_reader",
            principal_id=fixture.authorization.principal_id,
            scopes=fixture.authorization.scopes,
        ),
    )
    for foreign in foreign_authorizations:
        assert (
            actions.read_turn_proposal(
                foreign,
                conversation_id=fixture.conversation_id,
                turn_id=fixture.turn_id,
                mode=ConversationMode.MERCHANT,
            )
            is None
        )


def test_deterministic_checkout_id_rejects_corrupt_stored_identity(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "corruptproposalidentity", quantity=2)
    proposed = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
        request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
    )
    with postgres_sessions.begin() as session:
        proposal = session.get(V2Proposal, proposed.action.action_id)
        assert proposal is not None
        proposal.after_payload = {**proposal.after_payload, "total_vnd": 1}
    with pytest.raises(ActionStateConflictError, match="identity conflict"):
        actions.propose_checkout(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            lease_owner=fixture.lease_owner,
            request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
        )


def test_deterministic_direct_cart_id_rejects_corrupt_stored_identity(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture = _seed_shopper(postgres_sessions, "corruptcartidentity", quantity=1)
    request = CartChangeInput(
        cart_id=fixture.cart_id,
        product_id=fixture.product_id,
        quantity=3,
        expected_version=1,
    )
    result = actions.set_cart_item(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        request=request,
    )
    with postgres_sessions.begin() as session:
        proposal = session.get(V2Proposal, result.action_id)
        assert proposal is not None
        proposal.after_payload = {**proposal.after_payload, "quantity": 999}
    with pytest.raises(ActionStateConflictError, match="identity conflict"):
        actions.set_cart_item(
            fixture.authorization,
            conversation_id=fixture.conversation_id,
            turn_id=fixture.turn_id,
            request=request,
        )


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


def test_postgres_concurrent_shared_multi_offer_checkouts_lock_in_sorted_order(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    tenant_id = "tenant_multiofferconcurrency"
    offer_ids = ("offer_multioffer_a", "offer_multioffer_b")
    product_ids = (
        _product_id("multiofferconcurrencya"),
        _product_id("multiofferconcurrencyb"),
    )
    principals = ("principal_multioffer_a", "principal_multioffer_b")
    conversations = ("conversation_multioffer_a", "conversation_multioffer_b")
    turns = ("turn_multioffer_a", "turn_multioffer_b")
    carts = ("cart_multioffer_a", "cart_multioffer_b")
    with postgres_sessions.begin() as session:
        for product_id, offer_id in zip(product_ids, offer_ids, strict=True):
            _insert_product_offer(session, product_id, offer_id, tenant_id)
        for principal, conversation, turn, cart in zip(
            principals, conversations, turns, carts, strict=True
        ):
            _insert_context(
                session,
                conversation,
                turn,
                tenant_id,
                principal,
                ConversationMode.SHOPPER,
            )
            session.execute(
                text(
                    "INSERT INTO v2_carts "
                    "(id, tenant_id, principal_id, store_id, status, version) "
                    "VALUES (:id, :tenant, :principal, 'demo', 'active', 1)"
                ),
                {"id": cart, "tenant": tenant_id, "principal": principal},
            )
        line_rows = (
            ("line_a_2", carts[0], principals[0], offer_ids[1], 2),
            ("line_a_1", carts[0], principals[0], offer_ids[0], 1),
            ("line_b_1", carts[1], principals[1], offer_ids[0], 2),
            ("line_b_2", carts[1], principals[1], offer_ids[1], 3),
        )
        for line_id, cart, principal, offer_id, quantity in line_rows:
            session.execute(
                text(
                    "INSERT INTO v2_cart_lines "
                    "(id, cart_id, tenant_id, principal_id, store_id, offer_id, "
                    "quantity, offer_version) VALUES "
                    "(:id, :cart, :tenant, :principal, 'demo', :offer, :quantity, 1)"
                ),
                {
                    "id": line_id,
                    "cart": cart,
                    "tenant": tenant_id,
                    "principal": principal,
                    "offer": offer_id,
                    "quantity": quantity,
                },
            )
    authorizations = tuple(
        AuthorizationContext(
            tenant_id=tenant_id,
            principal_id=principal,
            scopes=frozenset({"ecommerce.read", "ecommerce.write"}),
        )
        for principal in principals
    )
    action_ids = []
    for authorization, conversation, turn, cart in zip(
        authorizations, conversations, turns, carts, strict=True
    ):
        proposal = actions.propose_checkout(
            authorization,
            conversation_id=conversation,
            turn_id=turn,
            lease_owner=f"worker_{turn}",
            request=CheckoutInput(cart_id=cart, expected_version=1),
        )
        action_ids.append(proposal.action.action_id)
        _finish_turn(postgres_sessions, turn)
    barrier = Barrier(2)

    def confirm(position: int) -> ActionStatus:
        barrier.wait(timeout=10)
        return actions.confirm_action(
            authorizations[position],
            action_id=action_ids[position],
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key=f"multi-offer-confirm-{position}",
        ).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(confirm, position) for position in (0, 1)]
        statuses = [future.result(timeout=10) for future in futures]
    assert sorted(status.value for status in statuses) == ["conflicted", "executed"]
    winner = statuses.index(ActionStatus.EXECUTED)
    expected_stocks = (9, 8) if winner == 0 else (8, 7)
    with postgres_sessions() as session:
        offers = {
            offer.id: offer
            for offer in session.scalars(
                select(V2Offer).where(V2Offer.id.in_(offer_ids)).order_by(V2Offer.id)
            )
        }
        assert (offers[offer_ids[0]].stock, offers[offer_ids[0]].version) == (
            expected_stocks[0],
            2,
        )
        assert (offers[offer_ids[1]].stock, offers[offer_ids[1]].version) == (
            expected_stocks[1],
            2,
        )
        orders = tuple(
            session.scalars(select(V2Order).where(V2Order.cart_id.in_(carts)))
        )
        assert len(orders) == 1
        assert orders[0].cart_id == carts[winner]
        proposals = {
            proposal.id: proposal
            for proposal in session.scalars(
                select(V2Proposal).where(V2Proposal.id.in_(action_ids))
            )
        }
        assert proposals[action_ids[winner]].status == ActionStatus.EXECUTED.value
        assert proposals[action_ids[1 - winner]].status == ActionStatus.CONFLICTED.value


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


def test_postgres_delete_conversation_serializes_with_confirm(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
) -> None:
    fixture, action_id = _checkout_proposal(
        actions, postgres_sessions, "deleteconfirmrace"
    )
    barrier = Barrier(2)

    def confirm() -> str:
        barrier.wait(timeout=10)
        try:
            return actions.confirm_action(
                fixture.authorization,
                action_id=action_id,
                request=ActionConfirmRequest(proposal_version=1),
                idempotency_key="delete-confirm-race-key",
            ).status.value
        except ResourceNotFoundError:
            return "not_found"

    def delete_conversation() -> str:
        barrier.wait(timeout=10)
        with postgres_sessions() as session:
            V2HistoryService(session).delete_conversation(
                fixture.authorization,
                fixture.conversation_id,
            )
        return "deleted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirm_future = executor.submit(confirm)
        delete_future = executor.submit(delete_conversation)
        confirm_outcome = confirm_future.result(timeout=20)
        delete_outcome = delete_future.result(timeout=20)

    assert delete_outcome == "deleted"
    with postgres_sessions() as session:
        conversation = session.get(V2Conversation, fixture.conversation_id)
        proposal = session.get(V2Proposal, action_id)
        order_count = session.scalar(
            select(func.count())
            .select_from(V2Order)
            .where(V2Order.cart_id == fixture.cart_id)
        )
        assert conversation is not None and conversation.deleted_at is not None
        assert proposal is not None
        if proposal.status == ActionStatus.EXECUTED.value:
            assert confirm_outcome == ActionStatus.EXECUTED.value
            assert order_count == 1
        else:
            assert proposal.status == ActionStatus.EXPIRED.value
            assert proposal.result == {"code": "conversation_deleted"}
            assert confirm_outcome == "not_found"
            assert order_count == 0


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


@pytest.mark.parametrize("mutation", ["price", "stock", "activity", "version"])
def test_postgres_merchant_confirmation_rejects_stale_offer_state(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
    mutation: str,
) -> None:
    fixture, action_id = _merchant_proposal(
        actions,
        postgres_sessions,
        f"merchantstale{mutation}",
        price=110_000,
    )
    with postgres_sessions.begin() as session:
        offer = session.get(V2Offer, fixture.offer_id)
        assert offer is not None
        if mutation == "price":
            offer.demo_price_vnd += 1
        elif mutation == "stock":
            offer.stock -= 1
        elif mutation == "activity":
            offer.is_active = False
        else:
            offer.version += 1
        changed_offer = (
            offer.demo_price_vnd,
            offer.stock,
            offer.version,
            offer.is_active,
        )

    first = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key=f"merchant-stale-{mutation}-key",
    )
    replay = actions.confirm_action(
        fixture.authorization,
        action_id=action_id,
        request=ActionConfirmRequest(proposal_version=1),
        idempotency_key=f"merchant-stale-{mutation}-key",
    )
    assert first.status == ActionStatus.CONFLICTED
    assert replay.model_dump(mode="json") == first.model_dump(mode="json")
    with postgres_sessions() as session:
        offer = session.get(V2Offer, fixture.offer_id)
        proposal = session.get(V2Proposal, action_id)
        audit_count = session.scalar(
            select(func.count())
            .select_from(V2ActionAudit)
            .where(V2ActionAudit.proposal_id == action_id)
        )
        assert offer is not None and proposal is not None
        assert (
            offer.demo_price_vnd,
            offer.stock,
            offer.version,
            offer.is_active,
        ) == changed_offer
        assert proposal.status == ActionStatus.CONFLICTED.value
        assert audit_count == 1


@pytest.mark.parametrize("same_key", [True, False])
def test_postgres_concurrent_merchant_confirmation_has_one_offer_effect(
    actions: V2ActionService,
    postgres_sessions: sessionmaker[Session],
    same_key: bool,
) -> None:
    suffix = "merchantconcurrentsame" if same_key else "merchantconcurrentdifferent"
    fixture, action_id = _merchant_proposal(
        actions,
        postgres_sessions,
        suffix,
        quantity_delta=-3,
    )
    barrier = Barrier(2)

    def confirm(position: int) -> dict[str, object]:
        barrier.wait(timeout=10)
        return actions.confirm_action(
            fixture.authorization,
            action_id=action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key=(
                "merchant-concurrent-shared"
                if same_key
                else f"merchant-concurrent-{position}"
            ),
        ).model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(confirm, position) for position in (0, 1)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    with postgres_sessions() as session:
        offer = session.get(V2Offer, fixture.offer_id)
        assert offer is not None
        assert (offer.stock, offer.version) == (7, 2)
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2ActionAudit)
                .where(V2ActionAudit.proposal_id == action_id)
            )
            == 1
        )
        assert session.scalar(
            select(func.count())
            .select_from(V2ActionIdempotency)
            .where(V2ActionIdempotency.proposal_id == action_id)
        ) == (1 if same_key else 2)


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
    _assert_checkout_effect(postgres_sessions, fixture, expected_stock=8)
    with postgres_sessions() as session:
        item_id = session.scalar(
            select(V2OrderItem.id).where(V2OrderItem.order_id == result.resource_id)
        )
    assert item_id is not None

    immutable_statements = (
        (
            "UPDATE v2_orders SET total_vnd = 1 WHERE id = :order_id",
            {"order_id": result.resource_id},
        ),
        (
            "DELETE FROM v2_orders WHERE id = :order_id",
            {"order_id": result.resource_id},
        ),
        (
            "UPDATE v2_order_items SET quantity = 1 WHERE id = :item_id",
            {"item_id": item_id},
        ),
        (
            "DELETE FROM v2_order_items WHERE id = :item_id",
            {"item_id": item_id},
        ),
    )
    for statement, parameters in immutable_statements:
        with pytest.raises(IntegrityError, match="immutable"):
            with postgres_sessions.begin() as session:
                session.execute(text(statement), parameters)
    _assert_checkout_effect(postgres_sessions, fixture, expected_stock=8)


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


class _ExpireBeforeProposalWriteService(V2ActionService):
    def _before_proposal_write(self, session: Session, turn: V2Turn) -> None:
        db_now = session.scalar(select(func.clock_timestamp()))
        assert db_now is not None
        turn.lease_expires_at = db_now - timedelta(seconds=1)
        session.flush()


class _BlockProposalCommitService(V2ActionService):
    def __init__(
        self, session_factory: sessionmaker[Session], *, catalog_version_id: str
    ) -> None:
        super().__init__(session_factory, catalog_version_id=catalog_version_id)
        self.commit_entered = Event()
        self.allow_commit = Event()

    def _before_commit(self) -> None:
        self.commit_entered.set()
        if not self.allow_commit.wait(timeout=10):
            raise TimeoutError("proposal commit was not released")


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
        lease_owner=fixture.lease_owner,
        request=CheckoutInput(cart_id=fixture.cart_id, expected_version=1),
    )
    assert proposal.action.action_id == proposal.action.proposal_id
    replay = actions.propose_checkout(
        fixture.authorization,
        conversation_id=fixture.conversation_id,
        turn_id=fixture.turn_id,
        lease_owner=fixture.lease_owner,
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
        lease_owner=fixture.lease_owner,
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
        lease_owner=f"worker_{turn_id}",
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
        lease_owner=f"worker_{turn_id}",
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
    _insert_running_turn(
        session,
        conversation_id,
        turn_id,
        tenant_id,
        principal_id,
        mode,
    )


def _insert_running_turn(
    session: Session,
    conversation_id: str,
    turn_id: str,
    tenant_id: str,
    principal_id: str,
    mode: ConversationMode,
) -> None:
    session.execute(
        text(
            "INSERT INTO v2_turns "
            "(id, conversation_id, tenant_id, principal_id, mode, store_id, "
            "client_turn_id, request_payload_hash, request_payload, execution_state, "
            "lease_owner, lease_expires_at) "
            "VALUES (:id, :conversation, :tenant, :principal, :mode, 'demo', "
            ":client, :hash, '{}', 'running', :lease_owner, "
            "now() + interval '5 minutes')"
        ),
        {
            "id": turn_id,
            "conversation": conversation_id,
            "tenant": tenant_id,
            "principal": principal_id,
            "mode": mode.value,
            "client": f"client-{turn_id}",
            "hash": "a" * 64,
            "lease_owner": f"worker_{turn_id}",
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
