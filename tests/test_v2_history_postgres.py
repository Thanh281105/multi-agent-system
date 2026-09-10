"""Real PostgreSQL coverage for durable v2 history and explicit memory."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext
from app.db.migrate import upgrade_database
from app.db.v2_repository import V2Repository
from app.models.v2 import (
    V2ActionIdempotency,
    V2Conversation,
    V2Preference,
    V2Proposal,
    V2Turn,
)
from app.v2.authorization import ResourceNotFoundError
from app.v2.contracts import (
    BudgetPreference,
    ConversationMode,
    DialogueOutcome,
    GenrePreference,
    PreferenceDeleteRequest,
    PreferenceKind,
    PreferencePutRequest,
    TurnResult,
    TurnStatus,
)
from app.v2.history import (
    ContextConstraint,
    PreferenceSourceError,
    V2HistoryService,
)
from tests.v2_postgres_support import disposable_postgres_database


@pytest.fixture(scope="module")
def postgres_sessions() -> Iterator[sessionmaker[Session]]:
    with disposable_postgres_database("thanh_v2_p2_history_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
        try:
            yield sessions
        finally:
            engine.dispose()


def test_history_is_owner_scoped_bounded_and_restartable(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_history", "principal_history", "ecommerce.read")
    foreign = _auth("tenant_other", "principal_history", "ecommerce.read")
    conversation_id = "conv_history_restart"

    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        for index in range(4):
            _complete_turn(
                session,
                owner,
                conversation_id=conversation_id,
                turn_id=f"turn_history_{index:04d}",
                client_turn_id=f"client-history-{index}",
                message=f"turn {index}",
                result=TurnResult(
                    outcome=DialogueOutcome.ANSWERED,
                    answer=f"answer {index}",
                ),
            )
        pending = repository.record_turn(
            owner,
            conversation_id=conversation_id,
            turn_id="turn_history_pending",
            client_turn_id="client-history-pending",
            payload={"message": "pending request"},
        )
        assert pending.execution_state == TurnStatus.PENDING.value

        service = V2HistoryService(session)
        turns = service.list_turns(owner, conversation_id, limit=3)
        assert [turn.user_message for turn in turns] == [
            "turn 2",
            "turn 3",
            "pending request",
        ]
        assert [
            turn.assistant_result.answer for turn in turns if turn.assistant_result
        ] == ["answer 2", "answer 3"]
        assert [turn.status for turn in turns] == [
            TurnStatus.COMPLETED,
            TurnStatus.COMPLETED,
            TurnStatus.PENDING,
        ]
        assert turns[-1].assistant_result is None
        assert turns[-1].error is None
        assert [
            item.conversation_id
            for item in service.list_conversations(owner, mode=ConversationMode.SHOPPER)
        ] == [conversation_id]

        with pytest.raises(ValueError):
            service.list_turns(owner, conversation_id, limit=0)
        with pytest.raises(ResourceNotFoundError):
            service.list_turns(foreign, conversation_id)

    # A new Session must observe the same committed transcript.
    with postgres_sessions() as session:
        turns = V2HistoryService(session).list_turns(owner, conversation_id, limit=3)
        assert [turn.user_message for turn in turns] == [
            "turn 2",
            "turn 3",
            "pending request",
        ]
        assert [turn.status for turn in turns] == [
            TurnStatus.COMPLETED,
            TurnStatus.COMPLETED,
            TurnStatus.PENDING,
        ]


def test_explicit_preferences_are_source_bound_owner_scoped_and_upserted(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth(
        "tenant_memory",
        "principal_memory",
        "ecommerce.read",
        "ecommerce.write",
    )
    foreign = _auth(
        "tenant_foreign",
        "principal_memory",
        "ecommerce.read",
        "ecommerce.write",
    )
    conversation_id = "conv_memory_source"

    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        first = _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_memory_first",
            client_turn_id="client-memory-first",
            message="remember history books",
        )
        second = _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_memory_second",
            client_turn_id="client-memory-second",
            message="remember a new genre",
        )

        service = V2HistoryService(session)
        genre_request = PreferencePutRequest(
            source_turn_id=first.id,
            preference=GenrePreference(
                kind=PreferenceKind.GENRE,
                value="Lịch sử",
            ),
        )
        first_record = service.put_preference(owner, genre_request)
        language_record = service.put_preference(
            owner,
            PreferencePutRequest(
                source_turn_id=first.id,
                preference={"kind": "language", "value": "vi"},
            ),
        )
        budget_record = service.put_preference(
            owner,
            PreferencePutRequest(
                source_turn_id=first.id,
                preference=BudgetPreference(
                    kind=PreferenceKind.MAX_BUDGET_VND,
                    value=200_000,
                ),
            ),
        )
        updated = service.put_preference(
            owner,
            PreferencePutRequest(
                source_turn_id=second.id,
                preference=GenrePreference(
                    kind=PreferenceKind.GENRE,
                    value="Khoa học",
                ),
            ),
        )

        assert updated.preference_id == first_record.preference_id
        assert updated.source_turn_id == second.id
        records = service.list_preferences(owner, mode=ConversationMode.SHOPPER)
        assert {record.preference.kind.value for record in records} == {
            "genre",
            "language",
            "max_budget_vnd",
        }
        assert {record.preference_id for record in records} == {
            first_record.preference_id,
            language_record.preference_id,
            budget_record.preference_id,
        }

        # Recording a turn does not infer or auto-write memory.
        _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_memory_third",
            client_turn_id="client-memory-third",
            message="I like biographies",
        )
        assert len(service.list_preferences(owner, mode=ConversationMode.SHOPPER)) == 3

        with pytest.raises(PreferenceSourceError):
            pending = repository.record_turn(
                owner,
                conversation_id=conversation_id,
                turn_id="turn_memory_pending",
                client_turn_id="client-memory-pending",
                payload={"message": "not completed"},
            )
            service.put_preference(
                owner,
                PreferencePutRequest(
                    source_turn_id=pending.id,
                    preference=genre_request.preference,
                ),
            )

        with pytest.raises(ResourceNotFoundError):
            service.put_preference(foreign, genre_request)

        service.delete_preference(
            owner,
            PreferenceDeleteRequest(preference_id=language_record.preference_id),
            mode=ConversationMode.SHOPPER,
        )
        assert [
            record.preference.kind.value
            for record in service.list_preferences(owner, mode=ConversationMode.SHOPPER)
        ] == ["genre", "max_budget_vnd"]

        with pytest.raises(ValidationError):
            PreferencePutRequest.model_validate(
                {
                    "source_turn_id": first.id,
                    "preference": {"kind": "political_belief", "value": "x"},
                }
            )


def test_model_context_is_bounded_and_current_request_wins(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth(
        "tenant_context",
        "principal_context",
        "ecommerce.read",
        "ecommerce.write",
    )
    conversation_id = "conv_context_current"

    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        source = _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_context_source",
            client_turn_id="client-context-source",
            message="find books in Vietnamese",
        )
        _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_context_second",
            client_turn_id="client-context-second",
            message="show another title",
        )
        _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_context_third",
            client_turn_id="client-context-third",
            message="compare them",
        )
        repository.record_turn(
            owner,
            conversation_id=conversation_id,
            turn_id="turn_context_pending",
            client_turn_id="client-context-pending",
            payload={"message": "pending current request"},
        )
        service = V2HistoryService(session)
        service.put_preference(
            owner,
            PreferencePutRequest(
                source_turn_id=source.id,
                preference={"kind": "language", "value": "vi"},
            ),
        )
        service.put_preference(
            owner,
            PreferencePutRequest(
                source_turn_id=source.id,
                preference=BudgetPreference(
                    kind=PreferenceKind.MAX_BUDGET_VND,
                    value=200_000,
                ),
            ),
        )

        context = service.build_model_context(
            owner,
            conversation_id,
            current_constraints=(
                ContextConstraint(key="max_budget_vnd", value=100_000),
                ContextConstraint(key="topic", value="history"),
            ),
            current_referenced_product_ids=(42, 7),
            relevant_preference_keys=(
                PreferenceKind.LANGUAGE,
                PreferenceKind.MAX_BUDGET_VND,
            ),
            turn_limit=2,
        )
        assert [turn.user_message for turn in context.recent_turns] == [
            "show another title",
            "compare them",
        ]
        assert all(turn.status is TurnStatus.COMPLETED for turn in context.recent_turns)
        assert context.referenced_product_ids == (42, 7)
        assert [record.preference.kind.value for record in context.preferences] == [
            "language",
            "max_budget_vnd",
        ]
        constraints = {item.key: item.value for item in context.active_constraints}
        assert constraints == {
            "language": "vi",
            "max_budget_vnd": 100_000,
            "topic": "history",
        }

    # Context rows and preference values survive a new Session.
    with postgres_sessions() as session:
        context = V2HistoryService(session).build_model_context(
            owner,
            conversation_id,
            current_constraints=(
                ContextConstraint(key="max_budget_vnd", value=100_000),
            ),
            current_referenced_product_ids=(42, 7),
            turn_limit=2,
        )
        assert len(context.recent_turns) == 2
        assert {item.key: item.value for item in context.active_constraints}[
            "max_budget_vnd"
        ] == 100_000


def test_delete_conversation_is_atomic_and_preserves_audit_rows(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth(
        "tenant_delete",
        "principal_delete",
        "ecommerce.read",
        "ecommerce.write",
    )
    foreign = _auth(
        "tenant_delete_other",
        "principal_delete",
        "ecommerce.read",
        "ecommerce.write",
    )
    conversation_id = "conv_delete_atomic"
    now = datetime.now(UTC)

    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        source = _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_delete_source",
            client_turn_id="client-delete-source",
            message="remember this",
        )
        service = V2HistoryService(session)
        service.put_preference(
            owner,
            PreferencePutRequest(
                source_turn_id=source.id,
                preference=GenrePreference(
                    kind=PreferenceKind.GENRE,
                    value="Lịch sử",
                ),
            ),
        )
        session.add_all(
            [
                V2Proposal(
                    id="proposal_delete_proposed",
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    mode="shopper",
                    store_id="demo",
                    conversation_id=conversation_id,
                    turn_id=source.id,
                    action_type="cart_change",
                    target_type="cart",
                    target_id="cart_delete",
                    target_version=1,
                    before_payload={},
                    after_payload={"quantity": 2},
                    proposal_version=1,
                    status="proposed",
                    expires_at=now + timedelta(minutes=10),
                ),
                V2Proposal(
                    id="proposal_delete_confirmed",
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    mode="shopper",
                    store_id="demo",
                    conversation_id=conversation_id,
                    turn_id=source.id,
                    action_type="cart_change",
                    target_type="cart",
                    target_id="cart_delete",
                    target_version=1,
                    before_payload={},
                    after_payload={"quantity": 3},
                    proposal_version=1,
                    status="confirmed",
                    expires_at=now + timedelta(minutes=10),
                ),
                V2Proposal(
                    id="proposal_delete_executed",
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    mode="shopper",
                    store_id="demo",
                    conversation_id=conversation_id,
                    turn_id=source.id,
                    action_type="cart_change",
                    target_type="cart",
                    target_id="cart_delete",
                    target_version=1,
                    before_payload={},
                    after_payload={"quantity": 4},
                    proposal_version=1,
                    status="executed",
                    expires_at=now + timedelta(minutes=10),
                ),
            ]
        )
        session.flush()
        session.add(
            V2ActionIdempotency(
                id="idempotency_delete_audit",
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                mode="shopper",
                store_id="demo",
                proposal_id="proposal_delete_executed",
                idempotency_key="delete-audit-key",
                payload_hash="a" * 64,
                status="committed",
                result={"executed": True},
            )
        )
        session.commit()

        with pytest.raises(ResourceNotFoundError):
            service.delete_conversation(foreign, conversation_id)

        summary = service.delete_conversation(owner, conversation_id)
        assert summary.conversation_id == conversation_id

    with postgres_sessions() as session:
        conversation = session.get(V2Conversation, conversation_id)
        assert conversation is not None and conversation.deleted_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Preference)
                .where(V2Preference.conversation_id == conversation_id)
            )
            == 0
        )

        proposals = {
            row.id: row
            for row in session.scalars(
                select(V2Proposal).where(V2Proposal.conversation_id == conversation_id)
            )
        }
        assert proposals["proposal_delete_proposed"].status == "expired"
        assert proposals["proposal_delete_proposed"].result == {
            "code": "conversation_deleted"
        }
        assert proposals["proposal_delete_confirmed"].status == "expired"
        assert proposals["proposal_delete_confirmed"].result == {
            "code": "conversation_deleted"
        }
        assert proposals["proposal_delete_executed"].status == "executed"
        assert session.get(V2ActionIdempotency, "idempotency_delete_audit") is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Turn)
                .where(V2Turn.conversation_id == conversation_id)
            )
            == 1
        )

        service = V2HistoryService(session)
        assert service.list_conversations(owner, mode=ConversationMode.SHOPPER) == ()
        with pytest.raises(ResourceNotFoundError):
            service.list_turns(owner, conversation_id)
        with pytest.raises(ResourceNotFoundError):
            service.build_model_context(owner, conversation_id)


def _complete_turn(
    session: Session,
    authorization: AuthorizationContext,
    *,
    conversation_id: str,
    turn_id: str,
    client_turn_id: str,
    message: str,
    result: TurnResult | None = None,
) -> V2Turn:
    repository = V2Repository(session)
    turn = repository.record_turn(
        authorization,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        payload={"message": message},
    )
    repository.claim_turn(
        authorization,
        turn.id,
        lease_owner=f"worker-{turn_id}",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    checked = result or TurnResult(
        outcome=DialogueOutcome.ANSWERED,
        answer=f"answer for {turn_id}",
    )
    return repository.complete_turn(
        authorization,
        turn.id,
        status=TurnStatus.COMPLETED,
        dialogue_outcome=checked.outcome,
        result={"turn_result": checked.model_dump(mode="json")},
    )


def _auth(tenant_id: str, principal_id: str, *scopes: str) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        scopes=frozenset(scopes),
    )
