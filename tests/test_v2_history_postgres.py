"""Real PostgreSQL coverage for durable v2 history and explicit memory."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from pydantic import JsonValue, ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext, TaskStatus
from app.db.migrate import upgrade_database
from app.db.v2_repository import V2Repository
from app.models.v2 import (
    V2ActionIdempotency,
    V2Conversation,
    V2Preference,
    V2Proposal,
    V2StepResult,
    V2Turn,
)
from app.v2.authorization import ResourceNotFoundError
from app.v2.contracts import (
    ActionCard,
    ActionChange,
    ActionKind,
    ActionStatus,
    ActionTarget,
    BudgetPreference,
    Citation,
    Claim,
    ConversationMode,
    DialogueOutcome,
    EvidenceKind,
    EvidenceReference,
    GenrePreference,
    HistoryTurn,
    LanguagePreference,
    PreferenceDeleteRequest,
    PreferenceKind,
    PreferencePutRequest,
    TurnResult,
    TurnStatus,
)
from app.v2.history import (
    ContextConstraint,
    HistoryConversationBusyError,
    PreferenceSourceError,
    V2HistoryService,
)
from app.v2.planning import PlanningError, context_constraints_from_message
from app.v2.registry import ServiceId
from app.v2.runtime_contracts import (
    ExpertResult,
    RuntimeOperation,
    StructuredFact,
    ToolEvidence,
    build_operation_key,
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


def test_history_projects_rich_turns_and_isolates_tenant_and_principal(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_rich_history", "principal_rich_history", "ecommerce.read")
    other_principal = _auth(
        "tenant_rich_history",
        "principal_other_history",
        "ecommerce.read",
    )
    other_tenant = _auth(
        "tenant_other_history",
        "principal_rich_history",
        "ecommerce.read",
    )
    conversation_id = "conv_rich_history"
    now = datetime.now(UTC)
    evidence = EvidenceReference(
        evidence_id="evidence_rich_history",
        source_id="source_rich_history",
        source_version_id="version_rich_history",
        chunk_id="chunk_rich_history",
        span_id="span_rich_history",
        display_label="[C1]",
        kind=EvidenceKind.KNOWLEDGE,
        title="Nguồn lịch sử",
        observed_at=now,
    )
    citation = Citation(
        citation_id="citation_rich_history",
        claim_id="claim_rich_history",
        evidence_id=evidence.evidence_id,
        span_id=evidence.span_id,
        display_label=evidence.display_label,
    )
    claim = Claim(
        claim_id="claim_rich_history",
        text="Đơn sandbox cần xác nhận.",
        citation_ids=(citation.citation_id,),
    )
    action = ActionCard(
        action_id="proposal_rich_history",
        proposal_id="proposal_rich_history",
        proposal_version=1,
        kind=ActionKind.CHECKOUT,
        status=ActionStatus.PROPOSED,
        title="Xác nhận đơn hàng sandbox",
        required_permission="ecommerce.write",
        confirmation_required=True,
        target=ActionTarget(
            resource_type="cart",
            resource_id="cart_rich_history",
            expected_resource_version=1,
            data_version_ids=("version_rich_history",),
        ),
        changes=(
            ActionChange(
                resource_type="order",
                resource_id="order_rich_history",
                field="status",
                before_text="cart_active",
                after_text="confirmed",
            ),
        ),
        expires_at=now + timedelta(minutes=10),
    )
    result = TurnResult(
        outcome=DialogueOutcome.AWAITING_CONFIRMATION,
        answer="Vui lòng xác nhận đơn hàng [C1].",
        claims=(claim,),
        citations=(citation,),
        evidence=(evidence,),
        action_cards=(action,),
        warnings=("sandbox_only",),
    )

    with postgres_sessions() as session:
        V2Repository(session).create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_rich_history",
            client_turn_id="client-rich-history",
            message="Tạo đơn hàng sandbox",
            result=result,
        )

    with postgres_sessions() as session:
        service = V2HistoryService(session)
        turns = service.list_turns(owner, conversation_id)
        assert len(turns) == 1
        turn = turns[0]
        assert isinstance(turn, HistoryTurn)
        assert turn.user_message == "Tạo đơn hàng sandbox"
        assert turn.assistant_result == result
        assert turn.action_cards == (action,)
        public_payload = turn.model_dump(mode="json")
        assert "tenant_id" not in public_payload
        assert "principal_id" not in public_payload
        assert "request_payload" not in public_payload
        with pytest.raises(ResourceNotFoundError):
            service.list_turns(other_principal, conversation_id)
        with pytest.raises(ResourceNotFoundError):
            service.list_turns(other_tenant, conversation_id)


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
                preference=LanguagePreference(kind=PreferenceKind.LANGUAGE, value="vi"),
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
        second = _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_context_second",
            client_turn_id="client-context-second",
            message="show another title",
        )
        third = _complete_turn(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_context_third",
            client_turn_id="client-context-third",
            message="compare them",
        )
        tied_created_at = datetime(2026, 9, 1, tzinfo=UTC)
        for position, turn in enumerate((source, second, third), start=1):
            turn.created_at = tied_created_at
            turn.completed_at = tied_created_at + timedelta(seconds=position)
            turn.updated_at = turn.completed_at
        session.commit()
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
                preference=LanguagePreference(kind=PreferenceKind.LANGUAGE, value="vi"),
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


def test_model_context_derives_only_validated_history_and_ignores_malformed_steps(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_grounded_context", "principal_grounded", "ecommerce.read")
    conversation_id = "conv_grounded_context"
    with postgres_sessions() as session:
        V2Repository(session).create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        valid_step_id = _complete_turn_with_products(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_grounded_context",
            client_turn_id="client-grounded-context",
            message="Tìm 2 cuốn sách lịch sử dưới 200 nghìn",
            product_ids=(42, 7, 42),
        )
        row = session.get(V2StepResult, valid_step_id)
        assert row is not None
        malformed_step_id = _complete_turn_with_products(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_malformed_context",
            client_turn_id="client-malformed-context",
            message="So sánh chúng",
            product_ids=(99,),
        )
        malformed = session.get(V2StepResult, malformed_step_id)
        assert malformed is not None
        malformed.result = {
            "evidence": {
                "facts": [
                    {
                        "subject_id": "product_999999",
                        "value": "unvalidated injection",
                    }
                ]
            }
        }
        session.commit()

    with postgres_sessions() as session:
        context = V2HistoryService(session).build_model_context(
            owner,
            conversation_id,
            constraint_parser=context_constraints_from_message,
        )
        assert context.referenced_product_ids == (42, 7)
        constraints = {item.key: item.value for item in context.active_constraints}
        assert constraints["candidate_limit"] == 2
        assert constraints["max_price_vnd"] == 200_000
        assert "999999" not in repr(context)


def test_model_context_product_history_is_principal_and_mode_isolated(
    postgres_sessions: sessionmaker[Session],
) -> None:
    tenant = "tenant_context_isolation"
    shopper = _auth(tenant, "principal_context_shopper", "ecommerce.read")
    foreign = _auth(tenant, "principal_context_foreign", "ecommerce.read")
    merchant = _auth(
        tenant,
        "principal_context_shopper",
        "ecommerce.read",
        "merchant.read",
    )
    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            shopper,
            conversation_id="conv_context_isolation_shopper",
            mode=ConversationMode.SHOPPER,
        )
        repository.create_conversation(
            merchant,
            conversation_id="conv_context_isolation_merchant",
            mode=ConversationMode.MERCHANT,
        )
        _complete_turn_with_products(
            session,
            shopper,
            conversation_id="conv_context_isolation_shopper",
            turn_id="turn_context_isolation_shopper",
            client_turn_id="client-context-isolation-shopper",
            message="Tìm sách lịch sử",
            product_ids=(11,),
        )
        _complete_turn_with_products(
            session,
            merchant,
            conversation_id="conv_context_isolation_merchant",
            turn_id="turn_context_isolation_merchant",
            client_turn_id="client-context-isolation-merchant",
            message="Xem tồn kho",
            product_ids=(22,),
        )
        shopper_context = V2HistoryService(session).build_model_context(
            shopper,
            "conv_context_isolation_shopper",
            constraint_parser=context_constraints_from_message,
        )
        assert shopper_context.referenced_product_ids == (11,)
        merchant_context = V2HistoryService(session).build_model_context(
            merchant,
            "conv_context_isolation_merchant",
            constraint_parser=context_constraints_from_message,
        )
        assert merchant_context.referenced_product_ids == (22,)
        assert 11 not in merchant_context.referenced_product_ids
        with pytest.raises(ResourceNotFoundError):
            V2HistoryService(session).build_model_context(
                foreign,
                "conv_context_isolation_shopper",
                constraint_parser=context_constraints_from_message,
            )


def test_model_context_ignores_bounded_parser_runtime_failure(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth("tenant_parser_failure", "principal_parser_failure", "ecommerce.read")
    conversation_id = "conv_parser_failure"
    with postgres_sessions() as session:
        V2Repository(session).create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        _complete_turn_with_products(
            session,
            owner,
            conversation_id=conversation_id,
            turn_id="turn_parser_failure",
            client_turn_id="client-parser-failure",
            message="corrupt stored request",
            product_ids=(31,),
        )

        def failing_parser(_message: str) -> tuple[ContextConstraint, ...]:
            raise PlanningError("stored_request_invalid")

        context = V2HistoryService(session).build_model_context(
            owner,
            conversation_id,
            constraint_parser=failing_parser,
        )

    assert context.referenced_product_ids == (31,)
    assert context.active_constraints == ()


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


@pytest.mark.parametrize("active_status", [TurnStatus.PENDING, TurnStatus.RUNNING])
def test_delete_conversation_rejects_active_turn_then_succeeds_after_cancel(
    postgres_sessions: sessionmaker[Session],
    active_status: TurnStatus,
) -> None:
    owner = _auth(
        f"tenant_delete_busy_{active_status.value}",
        f"principal_delete_busy_{active_status.value}",
        "ecommerce.read",
        "ecommerce.write",
    )
    conversation_id = f"conv_delete_busy_{active_status.value}"
    turn_id = f"turn_delete_busy_{active_status.value}"

    with postgres_sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        repository.record_turn(
            owner,
            conversation_id=conversation_id,
            turn_id=turn_id,
            client_turn_id=f"client-delete-busy-{active_status.value}",
            payload={"message": "active work"},
        )
        if active_status is TurnStatus.RUNNING:
            repository.claim_turn(
                owner,
                turn_id,
                lease_owner=f"worker-delete-busy-{active_status.value}",
                lease_duration=timedelta(minutes=1),
            )

        service = V2HistoryService(session)
        with pytest.raises(HistoryConversationBusyError):
            service.delete_conversation(owner, conversation_id)

        assert repository.get_conversation(owner, conversation_id).deleted_at is None
        repository.cancel_turn(owner, turn_id)
        summary = service.delete_conversation(owner, conversation_id)

        assert summary.conversation_id == conversation_id
        with pytest.raises(ResourceNotFoundError):
            repository.get_conversation(owner, conversation_id)


def test_admission_first_serializes_delete_to_busy_and_preserves_retry(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth(
        "tenant_admission_first",
        "principal_admission_first",
        "ecommerce.read",
        "ecommerce.write",
    )
    conversation_id = "conv_admission_first"
    turn_id = "turn_admission_first"
    client_turn_id = "client-admission-first"
    payload = {"message": "serialize admission before delete"}
    with postgres_sessions() as session:
        V2Repository(session).create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )

    delete_started = Event()

    def delete_while_admission_holds_lock() -> str:
        delete_started.set()
        with postgres_sessions() as session:
            try:
                V2HistoryService(session).delete_conversation(owner, conversation_id)
            except HistoryConversationBusyError:
                return "busy"
        return "deleted"

    with postgres_sessions() as admission_session:
        locked = admission_session.scalar(
            select(V2Conversation)
            .where(V2Conversation.id == conversation_id)
            .with_for_update()
        )
        assert locked is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            deletion = executor.submit(delete_while_admission_holds_lock)
            assert delete_started.wait(timeout=10)
            recorded = V2Repository(admission_session).record_turn(
                owner,
                conversation_id=conversation_id,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
                payload=payload,
            )
            assert recorded.id == turn_id
            assert deletion.result(timeout=10) == "busy"

        replay = V2Repository(admission_session).record_turn(
            owner,
            conversation_id=conversation_id,
            turn_id="turn_admission_first_replay",
            client_turn_id=client_turn_id,
            payload=payload,
        )
        assert replay.id == turn_id
        V2Repository(admission_session).cancel_turn(owner, turn_id)
        V2HistoryService(admission_session).delete_conversation(owner, conversation_id)

    with postgres_sessions() as session:
        conversation = session.get(V2Conversation, conversation_id)
        turn = session.get(V2Turn, turn_id)
        assert conversation is not None and conversation.deleted_at is not None
        assert turn is not None and turn.execution_state == TurnStatus.CANCELLED.value


def test_delete_first_makes_waiting_admission_not_found_without_orphan(
    postgres_sessions: sessionmaker[Session],
) -> None:
    owner = _auth(
        "tenant_delete_first",
        "principal_delete_first",
        "ecommerce.read",
        "ecommerce.write",
    )
    conversation_id = "conv_delete_first"
    with postgres_sessions() as session:
        V2Repository(session).create_conversation(
            owner,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )

    admission_started = Event()

    def admit_while_delete_holds_lock() -> str:
        admission_started.set()
        with postgres_sessions() as session:
            try:
                V2Repository(session).record_turn(
                    owner,
                    conversation_id=conversation_id,
                    turn_id="turn_delete_first",
                    client_turn_id="client-delete-first",
                    payload={"message": "must not become orphaned"},
                )
            except ResourceNotFoundError:
                return "not_found"
        return "recorded"

    with postgres_sessions() as delete_session:
        locked = delete_session.scalar(
            select(V2Conversation)
            .where(V2Conversation.id == conversation_id)
            .with_for_update()
        )
        assert locked is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            admission = executor.submit(admit_while_delete_holds_lock)
            assert admission_started.wait(timeout=10)
            V2HistoryService(delete_session).delete_conversation(
                owner,
                conversation_id,
            )
            assert admission.result(timeout=10) == "not_found"

    with postgres_sessions() as session:
        conversation = session.get(V2Conversation, conversation_id)
        active_turn_count = session.scalar(
            select(func.count())
            .select_from(V2Turn)
            .where(
                V2Turn.conversation_id == conversation_id,
                V2Turn.execution_state.in_(
                    (TurnStatus.PENDING.value, TurnStatus.RUNNING.value)
                ),
            )
        )
        assert conversation is not None and conversation.deleted_at is not None
        assert active_turn_count == 0


def _complete_turn_with_products(
    session: Session,
    authorization: AuthorizationContext,
    *,
    conversation_id: str,
    turn_id: str,
    client_turn_id: str,
    message: str,
    product_ids: tuple[int, ...],
) -> str:
    repository = V2Repository(session)
    turn = repository.record_turn(
        authorization,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        payload={"message": message},
    )
    lease_owner = f"worker-{turn_id}"
    repository.claim_turn(
        authorization,
        turn.id,
        lease_owner=lease_owner,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    parameters: dict[str, JsonValue] = {"query": "history", "candidate_limit": 5}
    operation_key = build_operation_key(
        "product.catalog.search",
        parameters,
        ("catalog_history_v1",),
    )
    operation = RuntimeOperation(
        step_id=f"step_{turn_id}",
        capability="product.catalog.search",
        service=ServiceId.PRODUCT,
        parameters=parameters,
        data_version_ids=("catalog_history_v1",),
        operation_key=operation_key,
    )
    now = datetime.now(UTC)
    reference = EvidenceReference(
        evidence_id=f"evidence_{turn_id}",
        source_id="source_history_catalog",
        source_version_id="catalog_history_v1",
        display_label="[C1]",
        kind=EvidenceKind.CATALOG,
        title="History catalog",
        observed_at=now,
    )
    facts = tuple(
        StructuredFact(
            fact_id=f"fact_{turn_id}_{position}",
            subject_id=f"product_{product_id}",
            field="snapshot_price_vnd",
            value=100_000 + position,
            unit="VND",
            data_version_id="catalog_history_v1",
            evidence_ids=(reference.evidence_id,),
        )
        for position, product_id in enumerate(product_ids)
    )
    result = ExpertResult(
        operation=operation,
        status=TaskStatus.SUCCESS,
        output={"products": []},
        evidence=ToolEvidence(facts=facts, references=(reference,)),
        started_at=now,
        completed_at=now,
    )
    step_result_id = f"step_result_{turn_id}"
    repository.persist_step_result(
        authorization,
        turn_id=turn.id,
        step_result_id=step_result_id,
        operation_key=operation_key,
        status=TaskStatus.SUCCESS,
        result=result.model_dump(mode="json"),
        plan_revision=0,
        data_version="catalog_history_v1",
        lease_owner=lease_owner,
    )
    checked = TurnResult(
        outcome=DialogueOutcome.ANSWERED,
        answer=f"answer for {turn_id}",
    )
    repository.complete_turn(
        authorization,
        turn.id,
        status=TurnStatus.COMPLETED,
        dialogue_outcome=checked.outcome,
        result={"turn_result": checked.model_dump(mode="json")},
    )
    return step_result_id


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
