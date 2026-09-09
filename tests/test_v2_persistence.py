"""Real PostgreSQL coverage for durable v2 ownership and retry semantics."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext, TaskStatus
from app.db.migrate import upgrade_database
from app.db.v2_repository import (
    TurnPayloadConflictError,
    TurnStateConflictError,
    V2Repository,
)
from app.v2.authorization import AuthorizationDeniedError, ResourceNotFoundError
from app.v2.contracts import (
    ConversationMode,
    DialogueOutcome,
    SafeExecutionError,
    TurnStatus,
)
from tests.v2_postgres_support import disposable_postgres_database


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    with disposable_postgres_database() as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def postgres_sessions(
    postgres_engine: Engine,
) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


def test_owner_tenant_isolation_and_revoked_scope(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_a", "principal_a", "ecommerce.read")
    other_principal = _auth("tenant_a", "principal_b", "ecommerce.read")
    other_tenant = _auth("tenant_b", "principal_a", "ecommerce.read")
    revoked = _auth("tenant_a", "principal_a")
    with postgres_sessions() as session:
        repository = V2Repository(session)
        conversation = repository.create_conversation(
            owner,
            conversation_id="conv_owner_isolation",
            mode=ConversationMode.SHOPPER,
            title="Synthetic owner test",
        )
        assert conversation.store_id == "demo"
        assert [
            item.id
            for item in repository.list_conversations(
                owner, mode=ConversationMode.SHOPPER
            )
        ] == [conversation.id]

        with pytest.raises(ResourceNotFoundError):
            repository.get_conversation(other_principal, conversation.id)
        with pytest.raises(ResourceNotFoundError):
            repository.get_conversation(other_tenant, conversation.id)
        with pytest.raises(AuthorizationDeniedError):
            repository.get_conversation(revoked, conversation.id)


def test_duplicate_turn_replay_and_payload_conflict(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_retry", "principal_retry", "ecommerce.read")
    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id="conv_retry_test",
            mode=ConversationMode.SHOPPER,
        )
        first = repository.record_turn(
            owner,
            conversation_id="conv_retry_test",
            turn_id="turn_retry_first",
            client_turn_id="client-retry-1",
            payload={"message": "Sách lịch sử", "filters": {"max": 200_000}},
        )
        replay = repository.record_turn(
            owner,
            conversation_id="conv_retry_test",
            turn_id="turn_retry_second",
            client_turn_id="client-retry-1",
            payload={"filters": {"max": 200_000}, "message": "Sách lịch sử"},
        )
        assert replay.id == first.id

        with pytest.raises(TurnPayloadConflictError):
            repository.record_turn(
                owner,
                conversation_id="conv_retry_test",
                turn_id="turn_retry_conflict",
                client_turn_id="client-retry-1",
                payload={"message": "Một nội dung khác"},
            )


def test_concurrent_same_payload_turn_retry_returns_one_row(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_race", "principal_race", "ecommerce.read")
    with postgres_sessions() as session:
        V2Repository(session).create_conversation(
            owner,
            conversation_id="conv_retry_race",
            mode=ConversationMode.SHOPPER,
        )

    barrier = Barrier(2)

    def record(turn_id: str) -> str:
        with postgres_sessions() as session:
            barrier.wait(timeout=10)
            return (
                V2Repository(session)
                .record_turn(
                    owner,
                    conversation_id="conv_retry_race",
                    turn_id=turn_id,
                    client_turn_id="client-race-1",
                    payload={"message": "same canonical payload"},
                )
                .id
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        turn_ids = tuple(executor.map(record, ("turn_race_first", "turn_race_second")))
    assert turn_ids[0] == turn_ids[1]

    with postgres_sessions() as session:
        count = session.scalar(
            text(
                "SELECT COUNT(*) FROM v2_turns "
                "WHERE conversation_id = 'conv_retry_race' "
                "AND client_turn_id = 'client-race-1'"
            )
        )
    assert count == 1


def test_turn_claim_and_terminal_transition_are_single_winner(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_transition", "principal_transition", "ecommerce.read")
    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id="conv_transition_race",
            mode=ConversationMode.SHOPPER,
        )
        repository.record_turn(
            owner,
            conversation_id="conv_transition_race",
            turn_id="turn_transition_race",
            client_turn_id="client-transition-1",
            payload={"message": "claim exactly once"},
        )

    claim_barrier = Barrier(2)

    def claim(worker: str) -> str:
        with postgres_sessions() as session:
            claim_barrier.wait(timeout=10)
            try:
                V2Repository(session).claim_turn(
                    owner,
                    "turn_transition_race",
                    lease_owner=worker,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            except TurnStateConflictError:
                return "conflict"
            return "claimed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_results = tuple(executor.map(claim, ("worker_a", "worker_b")))
    assert sorted(claim_results) == ["claimed", "conflict"]

    terminal_barrier = Barrier(2)

    def complete(answer: str) -> str:
        with postgres_sessions() as session:
            terminal_barrier.wait(timeout=10)
            try:
                V2Repository(session).complete_turn(
                    owner,
                    "turn_transition_race",
                    status=TurnStatus.COMPLETED,
                    dialogue_outcome=DialogueOutcome.ANSWERED,
                    result={"answer": answer},
                )
            except TurnStateConflictError:
                return "conflict"
            return "completed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminal_results = tuple(executor.map(complete, ("answer_a", "answer_b")))
    assert sorted(terminal_results) == ["completed", "conflict"]

    first_session = postgres_sessions()
    second_session = postgres_sessions()
    try:
        first_repository = V2Repository(first_session)
        stored = first_repository.get_turn(owner, "turn_transition_race")
        first_repository.complete_turn(
            owner,
            stored.id,
            status=TurnStatus.COMPLETED,
            dialogue_outcome=DialogueOutcome(stored.dialogue_outcome),
            result=stored.result,
        )
        second_session.execute(text("SET LOCAL lock_timeout = '1s'"))
        second_session.execute(
            text("SELECT id FROM v2_turns WHERE id = 'turn_transition_race' FOR UPDATE")
        ).one()
        second_session.commit()

        with pytest.raises(TurnStateConflictError):
            first_repository.complete_turn(
                owner,
                stored.id,
                status=TurnStatus.COMPLETED,
                dialogue_outcome=DialogueOutcome.ANSWERED,
                result={"answer": "different replay"},
            )
        second_session.execute(text("SET LOCAL lock_timeout = '1s'"))
        second_session.execute(
            text("SELECT id FROM v2_turns WHERE id = 'turn_transition_race' FOR UPDATE")
        ).one()
        second_session.commit()
    finally:
        first_session.close()
        second_session.close()


def test_step_and_terminal_results_are_durable_and_owner_scoped(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_result", "principal_result", "ecommerce.read")
    foreign = _auth("tenant_result", "principal_foreign", "ecommerce.read")
    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id="conv_result_test",
            mode=ConversationMode.SHOPPER,
        )
        turn = repository.record_turn(
            owner,
            conversation_id="conv_result_test",
            turn_id="turn_result_test",
            client_turn_id="client-result-1",
            payload={"message": "Tìm sách"},
        )
        repository.claim_turn(
            owner,
            turn.id,
            lease_owner="worker_result",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        step = repository.persist_step_result(
            owner,
            turn_id=turn.id,
            step_result_id="step_result_test",
            operation_key="catalog.search:sha256:test",
            status=TaskStatus.SUCCESS,
            result={"product_ids": [1, 2]},
            data_version="snapshot_test",
        )
        replayed_step = repository.persist_step_result(
            owner,
            turn_id=turn.id,
            step_result_id="step_result_replay",
            operation_key="catalog.search:sha256:test",
            status=TaskStatus.SUCCESS,
            result={"product_ids": [1, 2]},
            data_version="snapshot_test",
        )
        assert replayed_step.id == step.id

        completed = repository.complete_turn(
            owner,
            turn.id,
            status=TurnStatus.COMPLETED,
            dialogue_outcome=DialogueOutcome.ANSWERED,
            result={"answer": "Kết quả synthetic"},
        )
        assert completed.result == {"answer": "Kết quả synthetic"}
        assert repository.get_turn(owner, turn.id).completed_at is not None
        assert (
            repository.get_step_result(
                owner,
                turn_id=turn.id,
                operation_key="catalog.search:sha256:test",
            )
            is not None
        )
        with pytest.raises(ResourceNotFoundError):
            repository.get_turn(foreign, turn.id)


def test_failed_and_cancelled_turns_store_sql_null_result_shapes(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_nulls", "principal_nulls", "ecommerce.read")
    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id="conv_terminal_nulls",
            mode=ConversationMode.SHOPPER,
        )
        failed = repository.record_turn(
            owner,
            conversation_id="conv_terminal_nulls",
            turn_id="turn_failed_shape",
            client_turn_id="client-failed-shape",
            payload={"message": "synthetic failure"},
        )
        cancelled = repository.record_turn(
            owner,
            conversation_id="conv_terminal_nulls",
            turn_id="turn_cancelled_shape",
            client_turn_id="client-cancelled-shape",
            payload={"message": "synthetic cancellation"},
        )
        interrupted = repository.record_turn(
            owner,
            conversation_id="conv_terminal_nulls",
            turn_id="turn_interrupted_shape",
            client_turn_id="client-interrupted-shape",
            payload={"message": "synthetic interruption"},
        )
        repository.complete_turn(
            owner,
            failed.id,
            status=TurnStatus.FAILED,
            safe_error=SafeExecutionError(
                code="provider.timeout",
                message="Synthetic safe timeout",
                retryable=True,
            ),
        )
        repository.complete_turn(
            owner,
            cancelled.id,
            status=TurnStatus.CANCELLED,
        )
        repository.complete_turn(
            owner,
            interrupted.id,
            status=TurnStatus.INTERRUPTED,
            safe_error=SafeExecutionError(
                code="worker.interrupted",
                message="Synthetic safe interruption",
                retryable=True,
            ),
        )

    with postgres_sessions() as session:
        repository = V2Repository(session)
        restored_failure = repository.get_turn(owner, "turn_failed_shape")
        assert restored_failure.safe_error == {
            "code": "provider.timeout",
            "message": "Synthetic safe timeout",
            "retryable": True,
        }
        assert repository.get_turn(owner, "turn_interrupted_shape").safe_error == {
            "code": "worker.interrupted",
            "message": "Synthetic safe interruption",
            "retryable": True,
        }
        rows = session.execute(
            text(
                "SELECT id, result IS NULL, safe_error IS NULL FROM v2_turns "
                "WHERE id IN ('turn_failed_shape', 'turn_cancelled_shape', "
                "'turn_interrupted_shape') "
                "ORDER BY id"
            )
        ).all()
        assert rows == [
            ("turn_cancelled_shape", True, True),
            ("turn_failed_shape", True, False),
            ("turn_interrupted_shape", True, False),
        ]
        with pytest.raises(TurnStateConflictError):
            repository.claim_turn(
                owner,
                "turn_interrupted_shape",
                lease_owner="worker_reclaim_forbidden",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )


def test_postgres_constraints_block_cross_owner_and_immutable_changes(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO products "
                "(id, name, category, price, description, platform) "
                "VALUES (9901, 'Synthetic product', 'Books', 100000, "
                "'Synthetic test fixture', 'Tiki')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO v2_offers "
                "(id, tenant_id, store_id, product_id, demo_price_vnd, stock, version) "
                "VALUES ('offer_owner_a', 'tenant_constraint_a', 'demo', 9901, "
                "10000000000, 10, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO v2_carts "
                "(id, tenant_id, principal_id, store_id, status, version) "
                "VALUES ('cart_owner_b', 'tenant_constraint_b', 'principal_b', "
                "'demo', 'active', 1)"
            )
        )

    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO v2_cart_lines "
                    "(id, cart_id, tenant_id, principal_id, store_id, offer_id, "
                    "quantity, offer_version) VALUES "
                    "('line_cross_owner', 'cart_owner_b', 'tenant_constraint_b', "
                    "'principal_b', 'demo', 'offer_owner_a', 1, 1)"
                )
            )

    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO v2_offers "
                    "(id, tenant_id, store_id, product_id, demo_price_vnd, "
                    "stock, version) "
                    "VALUES ('offer_invalid_price', 'tenant_constraint_a', 'demo', "
                    "9901, 10000000001, 10, 1)"
                )
            )

    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE v2_conversations SET mode = 'merchant' "
                    "WHERE id = 'conv_owner_isolation'"
                )
            )

    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO v2_carts "
                "(id, tenant_id, principal_id, store_id, status, version) VALUES "
                "('cart_order_owner', 'tenant_constraint_a', 'principal_order', "
                "'demo', 'checked_out', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO v2_orders "
                "(id, tenant_id, principal_id, store_id, cart_id, cart_version, "
                "status, total_vnd) VALUES "
                "('order_immutable', 'tenant_constraint_a', 'principal_order', "
                "'demo', 'cart_order_owner', 1, 'confirmed', 10000000000)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO v2_order_items "
                "(id, order_id, tenant_id, principal_id, store_id, offer_id, "
                "product_id, quantity, unit_price_vnd, product_snapshot) VALUES "
                "('order_item_immutable', 'order_immutable', 'tenant_constraint_a', "
                "'principal_order', 'demo', 'offer_owner_a', 9901, 1, "
                '10000000000, \'{"name": "Synthetic product"}\')'
            )
        )

    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text("UPDATE v2_orders SET total_vnd = 1 WHERE id = 'order_immutable'")
            )
    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM v2_order_items WHERE id = 'order_item_immutable'")
            )


def test_vectors_are_bound_to_their_index_fingerprint_corpus(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO v2_knowledge_corpus_versions "
                "(id, corpus_name, version, status) VALUES "
                "('corpus_vector_a', 'synthetic', 'a', 'published'), "
                "('corpus_vector_b', 'synthetic', 'b', 'published')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO v2_knowledge_index_manifests "
                "(id, corpus_version_id, fingerprint, embedding_model, "
                "embedding_dimension, chunker_version, enrichment_policy_version) "
                "VALUES ('index_vector_b', 'corpus_vector_b', :fingerprint, "
                "'hashing', 2, 'chunker_test', 'none')"
            ),
            {"fingerprint": "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO v2_knowledge_documents "
                "(id, corpus_version_id, source_url, title, use_basis, content_hash, "
                "document_version, retrieved_at) VALUES "
                "('document_vector_a', 'corpus_vector_a', 'https://example.test/a', "
                "'Synthetic source', 'public metadata', :content_hash, '1', now())"
            ),
            {"content_hash": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO v2_knowledge_chunks "
                "(id, document_id, corpus_version_id, chunk_index, content, "
                "token_count) "
                "VALUES ('chunk_vector_a', 'document_vector_a', 'corpus_vector_a', "
                "0, 'Synthetic chunk', 2)"
            )
        )

    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO v2_knowledge_vectors "
                    "(id, chunk_id, index_manifest_id, corpus_version_id, vector) "
                    "VALUES ('vector_cross_fingerprint', 'chunk_vector_a', "
                    "'index_vector_b', 'corpus_vector_a', '[0.1, 0.2]')"
                )
            )


def _auth(tenant_id: str, principal_id: str, *scopes: str) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        scopes=frozenset(scopes),
    )


def test_disposable_database_prefix_cannot_exceed_postgres_identifier_limit() -> None:
    with pytest.raises(ValueError):
        with disposable_postgres_database("thanh_v2_p2_" + "x" * 20):
            pass
