"""Real PostgreSQL gate for claimed, restart-safe v2 read execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext, TaskStatus
from app.db import v2_repository as v2_repository_module
from app.db.migrate import upgrade_database
from app.db.v2_repository import (
    TurnLeaseConflictError,
    TurnRuntimeConflictError,
    TurnStateConflictError,
    V2Repository,
)
from app.models.v2 import (
    V2Cart,
    V2KnowledgeCorpusVersion,
    V2Order,
    V2Proposal,
    V2StepResult,
    V2Turn,
)
from app.shared import ModelCallMetadata, ModelRuntimeError
from app.shared.budget import (
    PricingManifest,
    ProviderBudgetContext,
    ProviderUsage,
    SQLProviderBudgetLedger,
    current_provider_budget,
    default_pricing_manifest_path,
)
from app.v2 import execution as v2_execution_module
from app.v2.actions import V2ActionService
from app.v2.answers import GroundedAnswerProducer
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import (
    ActionConfirmRequest,
    ActionRejectRequest,
    ActionStatus,
    BudgetPreference,
    ConversationMode,
    DialogueOutcome,
    PreferenceKind,
    PreferencePutRequest,
    SafeExecutionError,
    TurnResult,
    TurnStatus,
)
from app.v2.execution import (
    DurableExecutionError,
    DurableOperationExecutor,
    DurableReadTurnExecutor,
    DurableTurnOutcome,
    DurableTurnRequest,
    TurnComputation,
    TurnExecutionInterrupted,
)
from app.v2.history import V2HistoryService
from app.v2.planning import (
    BoundedV2Planner,
    PlannedTurn,
    PlanningContext,
    RuntimeDataVersions,
)
from app.v2.registry import CheckoutInput, ProductResult, ProposalResult
from app.v2.runtime_contracts import ExpertResult, RuntimeOperation, ToolEvidence
from app.v2.supervisor import V2ReadSupervisor
from tests.test_v2_tools import read_tools, seed_tool_catalog, tool_access
from tests.v2_postgres_support import disposable_postgres_database


@dataclass(frozen=True, slots=True)
class _RuntimeStore:
    sessions: sessionmaker[Session]
    ledger: SQLProviderBudgetLedger


class _CountingActionService(V2ActionService):
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        catalog_version_id: str,
    ) -> None:
        super().__init__(sessions, catalog_version_id=catalog_version_id)
        self.proposal_calls = 0

    def propose_checkout(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        lease_owner: str,
        request: CheckoutInput,
    ) -> ProposalResult:
        self.proposal_calls += 1
        return super().propose_checkout(
            authorization,
            conversation_id=conversation_id,
            turn_id=turn_id,
            lease_owner=lease_owner,
            request=request,
        )


class _CountingPlanner(BoundedV2Planner):
    def __init__(self) -> None:
        super().__init__(runtime_mode="off")
        self.calls = 0

    async def plan(self, message: str, context: PlanningContext) -> PlannedTurn:
        self.calls += 1
        return await super().plan(message, context)


class _NeverAnswerProducer:
    def __init__(self) -> None:
        self.calls = 0

    async def produce(self, **_: Any) -> Any:
        self.calls += 1
        raise AssertionError("proposal turns must not dispatch grounding")


@pytest.fixture(scope="module")
def postgres_runtime_store() -> Iterator[_RuntimeStore]:
    with disposable_postgres_database("thanh_v2_p2_runtime_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
        with sessions() as session:
            seed_tool_catalog(session)
        with sessions() as session:
            session.add_all(
                [
                    V2KnowledgeCorpusVersion(
                        id="corpus_runtime_a",
                        corpus_name="runtime-fixture",
                        version="a",
                        status="published",
                        manifest={},
                        published_at=datetime.now(UTC),
                    ),
                    V2KnowledgeCorpusVersion(
                        id="corpus_runtime_b",
                        corpus_name="runtime-fixture",
                        version="b",
                        status="published",
                        manifest={},
                        published_at=datetime.now(UTC),
                    ),
                ]
            )
            session.commit()
        ledger = SQLProviderBudgetLedger(
            sessions,
            PricingManifest.load(default_pricing_manifest_path()),
        )
        ledger.create_account(account_id="p4-runtime-account")
        try:
            yield _RuntimeStore(sessions=sessions, ledger=ledger)
        finally:
            engine.dispose()


class _SuccessHandler:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.calls = 0

    async def run_claimed(self, **kwargs: Any) -> TurnComputation:
        self.calls += 1
        turn_id = kwargs["turn_id"]
        budget = current_provider_budget()
        assert budget is not None and budget.scope_id == turn_id
        with self.sessions() as session:
            # A row lock succeeds here only because record/claim committed before
            # the handler and no transaction spans this provider boundary.
            turn = session.scalar(
                select(V2Turn).where(V2Turn.id == turn_id).with_for_update()
            )
            assert turn is not None
            assert turn.execution_state == TurnStatus.RUNNING.value
            assert turn.runtime_metadata["data_versions"]["corpus_version_id"] == (
                "corpus_runtime_a"
            )
            session.rollback()
        return _answered("Durable synthetic result")


@pytest.mark.asyncio
async def test_record_claim_precede_handler_and_completed_retry_reuses_result(
    postgres_runtime_store: _RuntimeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_replay")
    handler = _SuccessHandler(store.sessions)
    runner = DurableReadTurnExecutor(
        store.sessions,
        handler,
        budget_ledger=store.ledger,
    )
    budget = _budget(store, "turn_runtime_replay")
    first = await runner.execute(
        _request(
            "conv_runtime_replay",
            "turn_runtime_replay",
            "client-runtime-replay",
            "worker-runtime-first",
        ),
        context,
        provider_budget=budget,
    )

    def reject_history(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("completed replay must not load history")

    monkeypatch.setattr(V2HistoryService, "build_model_context", reject_history)
    retry = await runner.execute(
        _request(
            "conv_runtime_replay",
            "turn_runtime_replay_new_id",
            "client-runtime-replay",
            "worker-runtime-retry",
        ),
        context,
        provider_budget=budget,
    )
    assert first.status == retry.status == TurnStatus.COMPLETED
    assert first.result == retry.result
    assert retry.turn_id == "turn_runtime_replay"
    assert retry.reused is True
    assert handler.calls == 1


class _UsageHandler:
    def __init__(self, ledger: SQLProviderBudgetLedger) -> None:
        self.ledger = ledger
        self.calls = 0

    async def run_claimed(self, **kwargs: Any) -> TurnComputation:
        self.calls += 1
        scope_id = kwargs["turn_id"]
        known = self.ledger.reserve_attempt(
            scope_id=scope_id,
            call_id="mcall_runtime_usage_known_00000001",
            attempt_number=1,
            operation="generation",
            purpose="chat",
            model="gpt-5.4-mini",
            request_fingerprint_sha256="a" * 64,
            input_token_bound=20,
            output_token_bound=4,
        )
        assert self.ledger.mark_dispatched(
            scope_id=scope_id,
            attempt_id=known.attempt_id,
        )
        self.ledger.settle_known_usage(
            scope_id=scope_id,
            attempt_id=known.attempt_id,
            usage=ProviderUsage(
                input_tokens=11,
                cached_input_tokens=2,
                output_tokens=3,
                reasoning_tokens=1,
                total_tokens=14,
            ),
            response_id="resp_runtime_usage_known",
            response_model="gpt-5.4-mini-2026-03-17",
            response_service_tier="default",
        )
        self.ledger.record_attempt_result(
            scope_id=scope_id,
            attempt_id=known.attempt_id,
            result_status="success",
        )
        unknown = self.ledger.reserve_attempt(
            scope_id=scope_id,
            call_id="mcall_runtime_usage_unknown_000001",
            attempt_number=1,
            operation="generation",
            purpose="chat",
            model="gpt-5.4-mini",
            request_fingerprint_sha256="b" * 64,
            input_token_bound=20,
            output_token_bound=4,
        )
        assert self.ledger.mark_dispatched(
            scope_id=scope_id,
            attempt_id=unknown.attempt_id,
        )
        self.ledger.settle_unknown_usage(
            scope_id=scope_id,
            attempt_id=unknown.attempt_id,
            result_status="timeout",
            error_code="model_timeout",
        )
        return _answered("Usage-bearing durable result")


@pytest.mark.asyncio
async def test_completed_replay_reads_authoritative_usage_without_live_budget(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_usage")
    handler = _UsageHandler(store.ledger)
    first = await DurableReadTurnExecutor(
        store.sessions,
        handler,
        budget_ledger=store.ledger,
    ).execute(
        _request(
            "conv_runtime_usage",
            "turn_runtime_usage",
            "client-runtime-usage",
            "worker-runtime-usage",
        ),
        context,
        provider_budget=_budget(store, "turn_runtime_usage"),
    )
    never = _NeverHandler()
    replay = await DurableReadTurnExecutor(
        store.sessions,
        never,
        budget_ledger=store.ledger,
    ).execute(
        _request(
            "conv_runtime_usage",
            "turn_runtime_usage_replay",
            "client-runtime-usage",
            "worker-runtime-usage-replay",
        ),
        context,
    )
    authoritative = store.ledger.scope_usage_summary("turn_runtime_usage")
    assert first.status == replay.status == TurnStatus.COMPLETED
    assert replay.reused is True
    assert replay.usage.input_tokens == authoritative.input_tokens == 11
    assert replay.usage.output_tokens == authoritative.output_tokens == 3
    assert replay.usage.total_tokens == authoritative.total_tokens == 14
    assert replay.usage.provider_attempts == authoritative.provider_attempts == 2
    assert replay.usage.generation_calls == 2
    assert replay.usage.unknown_usage_attempts == 1
    assert replay.usage.known_cost_usd == authoritative.costs.known_usd
    assert replay.usage.unknown_reserved_cost_usd == authoritative.costs.unknown_usd
    assert never.calls == 0


class _BlockingHandler:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def run_claimed(self, **_: Any) -> TurnComputation:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return _answered("Single claimant completed")


class _FarFutureClock:
    @staticmethod
    def now(tz: object | None = None) -> datetime:
        value = datetime.now(UTC) + timedelta(days=30)
        return value if tz is not None else value.replace(tzinfo=None)


class _FarPastClock:
    @staticmethod
    def now(tz: object | None = None) -> datetime:
        value = datetime(2000, 1, 1, tzinfo=UTC)
        return value if tz is not None else value.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_concurrent_retry_has_one_claimant_and_no_duplicate_handler(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_claim")
    handler = _BlockingHandler()
    runner = DurableReadTurnExecutor(store.sessions, handler)
    first_task = asyncio.create_task(
        runner.execute(
            _request(
                "conv_runtime_claim",
                "turn_runtime_claim",
                "client-runtime-claim",
                "worker-runtime-a",
            ),
            context,
        )
    )
    await asyncio.wait_for(handler.entered.wait(), timeout=5)
    second = await runner.execute(
        _request(
            "conv_runtime_claim",
            "turn_runtime_claim_retry",
            "client-runtime-claim",
            "worker-runtime-b",
        ),
        context,
    )
    assert second.status == TurnStatus.RUNNING
    assert second.reused is True
    assert handler.calls == 1
    handler.release.set()
    first = await asyncio.wait_for(first_task, timeout=5)
    assert first.status == TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_claim_lifetime_is_relative_to_locked_database_clock_under_app_skew(
    postgres_runtime_store: _RuntimeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = postgres_runtime_store
    context = _context()
    conversation_id = "conv_runtime_db_clock_claim"
    turn_id = "turn_runtime_db_clock_claim"
    _conversation(store.sessions, context, conversation_id)
    handler = _BlockingHandler()
    runner = DurableReadTurnExecutor(store.sessions, handler)

    with monkeypatch.context() as clock:
        clock.setattr(v2_execution_module, "datetime", _FarFutureClock)
        clock.setattr(
            v2_repository_module,
            "utc_now",
            lambda: datetime.now(UTC) + timedelta(days=30),
        )
        task = asyncio.create_task(
            runner.execute(
                _request(
                    conversation_id,
                    turn_id,
                    "client-runtime-db-clock-claim",
                    "worker-runtime-db-clock-claim",
                ),
                context,
            )
        )
        await asyncio.wait_for(handler.entered.wait(), timeout=5)
        with store.sessions() as session:
            turn = session.get(V2Turn, turn_id)
            db_now = session.scalar(select(func.clock_timestamp()))
            assert turn is not None and db_now is not None
            assert turn.lease_expires_at is not None
            remaining = (turn.lease_expires_at - db_now).total_seconds()
            assert 55 < remaining <= 65

    handler.release.set()
    outcome = await asyncio.wait_for(task, timeout=5)
    assert outcome.status == TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_active_running_retry_skips_proposal_recovery(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    conversation_id = "conv_runtime_active_recovery_skip"
    turn_id = "turn_runtime_active_recovery_skip"
    client_turn_id = "client-runtime-active-recovery-skip"
    _conversation(store.sessions, context, conversation_id)
    _record_bind_claim(
        store.sessions,
        context,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        lease_owner="worker-runtime-active",
    )
    handler = _RecoveryTrackingNeverHandler()
    outcome = await DurableReadTurnExecutor(store.sessions, handler).execute(
        _request(
            conversation_id,
            "turn_runtime_active_recovery_retry",
            client_turn_id,
            "worker-runtime-active-retry",
        ),
        context,
    )
    assert outcome.status == TurnStatus.RUNNING
    assert outcome.reused is True
    assert handler.calls == 0
    assert handler.recovery_calls == 0


@pytest.mark.asyncio
async def test_expired_running_turn_is_interrupted_and_never_reclaimed(
    postgres_runtime_store: _RuntimeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = postgres_runtime_store
    context = _context()
    conversation_id = "conv_runtime_expired"
    turn_id = "turn_runtime_expired"
    client_turn_id = "client-runtime-expired"
    _conversation(store.sessions, context, conversation_id)
    _record_bind_claim(
        store.sessions,
        context,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        lease_owner="worker-runtime-dead",
    )
    with store.sessions() as session:
        session.execute(
            update(V2Turn)
            .where(V2Turn.id == turn_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()
    handler = _NeverHandler()

    def reject_history(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("expired running replay must not load history")

    monkeypatch.setattr(V2HistoryService, "build_model_context", reject_history)
    runner = DurableReadTurnExecutor(store.sessions, handler)
    outcome = await runner.execute(
        _request(
            conversation_id,
            "turn_runtime_expired_retry",
            client_turn_id,
            "worker-runtime-restart",
        ),
        context,
    )
    replay = await runner.execute(
        _request(
            conversation_id,
            "turn_runtime_expired_again",
            client_turn_id,
            "worker-runtime-restart-two",
        ),
        context,
    )
    assert outcome.status == replay.status == TurnStatus.INTERRUPTED
    assert outcome.error is not None and outcome.error.code == "turn_lease_expired"
    assert handler.calls == 0
    with store.sessions() as session:
        turn = session.get(V2Turn, turn_id)
        assert turn is not None
        assert turn.execution_state == TurnStatus.INTERRUPTED.value
        assert turn.lease_owner is None
        assert turn.lease_expires_at is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(V2Proposal)
                .where(V2Proposal.turn_id == turn_id)
            )
            == 0
        )


@pytest.mark.parametrize("write_kind", ["checkpoint", "completion"])
def test_expired_active_write_fences_ignore_slow_application_clock(
    postgres_runtime_store: _RuntimeStore,
    monkeypatch: pytest.MonkeyPatch,
    write_kind: str,
) -> None:
    store = postgres_runtime_store
    context = _context()
    suffix = write_kind.replace("completion", "complete")
    conversation_id = f"conv_runtime_db_clock_{suffix}"
    turn_id = f"turn_runtime_db_clock_{suffix}"
    lease_owner = f"worker-runtime-db-clock-{suffix}"
    _conversation(store.sessions, context, conversation_id)
    _record_bind_claim(
        store.sessions,
        context,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=f"client-runtime-db-clock-{suffix}",
        lease_owner=lease_owner,
    )
    with store.sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_turns SET lease_expires_at = "
                "clock_timestamp() - interval '1 second' WHERE id = :turn_id"
            ),
            {"turn_id": turn_id},
        )

    monkeypatch.setattr(
        v2_repository_module,
        "utc_now",
        lambda: datetime(2000, 1, 1, tzinfo=UTC),
    )
    with store.sessions() as session:
        repository = V2Repository(session)
        if write_kind == "checkpoint":
            with pytest.raises(TurnRuntimeConflictError, match="lease"):
                repository.checkpoint_turn_runtime(
                    _authorization(context),
                    turn_id,
                    lease_owner=lease_owner,
                    knowledge_retrievals=1,
                )
        else:
            with pytest.raises(TurnLeaseConflictError, match="current"):
                repository.complete_turn(
                    _authorization(context),
                    turn_id,
                    status=TurnStatus.COMPLETED,
                    dialogue_outcome=DialogueOutcome.ANSWERED,
                    result={"stale_worker": True},
                    lease_owner=lease_owner,
                    expected_status=TurnStatus.RUNNING,
                )

    with store.sessions() as session:
        turn = session.get(V2Turn, turn_id)
        assert turn is not None
        assert turn.execution_state == TurnStatus.RUNNING.value
        assert turn.runtime_metadata["knowledge_retrievals"] == 0
        assert turn.result is None


@pytest.mark.asyncio
async def test_active_claim_check_uses_database_clock_before_dispatch(
    postgres_runtime_store: _RuntimeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = postgres_runtime_store
    context = _context()
    conversation_id = "conv_runtime_db_clock_assert"
    turn_id = "turn_runtime_db_clock_assert"
    lease_owner = "worker-runtime-db-clock-assert"
    _conversation(store.sessions, context, conversation_id)
    _record_bind_claim(
        store.sessions,
        context,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id="client-runtime-db-clock-assert",
        lease_owner=lease_owner,
    )
    with store.sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_turns SET lease_expires_at = "
                "clock_timestamp() - interval '1 second' WHERE id = :turn_id"
            ),
            {"turn_id": turn_id},
        )
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Tìm sách Sapiens",
        context,
    )
    dispatcher = _CatalogDispatcher()
    monkeypatch.setattr(v2_execution_module, "datetime", _FarPastClock)
    monkeypatch.setattr(
        v2_repository_module,
        "utc_now",
        lambda: datetime(2000, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(TurnExecutionInterrupted, match="turn_lease_expired"):
        await DurableOperationExecutor(store.sessions, dispatcher).execute(
            turn_id=turn_id,
            lease_owner=lease_owner,
            access=context.access,
            operations=planned.initial_operations,
            plan_revision=0,
        )
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_expired_recovery_cannot_overwrite_renewed_live_lease(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    conversation_id = "conv_runtime_expired_renewed"
    turn_id = "turn_runtime_expired_renewed"
    client_turn_id = "client-runtime-expired-renewed"
    _conversation(store.sessions, context, conversation_id)
    _record_bind_claim(
        store.sessions,
        context,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        lease_owner="worker-runtime-expired",
    )
    with store.sessions.begin() as session:
        session.execute(
            text(
                "UPDATE v2_turns SET lease_expires_at = "
                "clock_timestamp() - interval '1 second' WHERE id = :turn_id"
            ),
            {"turn_id": turn_id},
        )

    handler = _NeverHandler()
    runner = _RenewLeaseBeforeInterruptExecutor(store.sessions, handler)
    outcome = await runner.execute(
        _request(
            conversation_id,
            "turn_runtime_expired_renewed_retry",
            client_turn_id,
            "worker-runtime-retry",
        ),
        context,
    )
    assert outcome.status == TurnStatus.RUNNING
    assert handler.calls == 0
    with store.sessions() as session:
        turn = session.get(V2Turn, turn_id)
        db_now = session.scalar(select(func.clock_timestamp()))
        assert turn is not None and db_now is not None
        assert turn.execution_state == TurnStatus.RUNNING.value
        assert turn.lease_owner == "worker-runtime-renewed"
        assert turn.lease_expires_at is not None and turn.lease_expires_at > db_now
        assert turn.safe_error is None


@pytest.mark.asyncio
async def test_pending_retry_with_different_runtime_pins_fails_before_handler(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context_a = _context()
    context_b = _context(corpus="corpus_runtime_b", index="index_runtime_b")
    conversation_id = "conv_runtime_pin"
    turn_id = "turn_runtime_pin"
    client_turn_id = "client-runtime-pin"
    _conversation(store.sessions, context_a, conversation_id)
    authorization = _authorization(context_a)
    with store.sessions() as session:
        repository = V2Repository(session)
        repository.record_turn(
            authorization,
            conversation_id=conversation_id,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            payload=_stable_payload(conversation_id, client_turn_id),
            corpus_version_id=context_a.versions.corpus_version_id,
        )
        repository.bind_turn_runtime(
            authorization,
            turn_id,
            data_versions=context_a.versions.model_dump(mode="json"),
        )
    handler = _NeverHandler()
    outcome = await DurableReadTurnExecutor(store.sessions, handler).execute(
        _request(
            conversation_id,
            "turn_runtime_pin_retry",
            client_turn_id,
            "worker-runtime-pin",
        ),
        context_b,
    )
    assert outcome.status == TurnStatus.INTERRUPTED
    assert outcome.error is not None
    assert outcome.error.code == "turn_runtime_pin_conflict"
    assert handler.calls == 0


class _CatalogDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, operation: RuntimeOperation, access: object
    ) -> ExpertResult:
        del access
        self.calls += 1
        now = datetime.now(UTC)
        return ExpertResult(
            operation=operation,
            status=TaskStatus.SUCCESS,
            output=ProductResult(products=()).model_dump(mode="json"),
            evidence=ToolEvidence(),
            started_at=now,
            completed_at=now,
        )


class _CrashingExpertReasoner:
    async def enrich(self, result: ExpertResult) -> ExpertResult:
        del result
        raise RuntimeError("synthetic expert crash")


class _BlockingExpertReasoner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def enrich(self, result: ExpertResult) -> ExpertResult:
        self.entered.set()
        await asyncio.Event().wait()
        return result


@pytest.mark.asyncio
async def test_unexpected_expert_failure_preserves_completed_read_as_partial(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_expert_crash")
    dispatcher = _CatalogDispatcher()
    operation_executor = DurableOperationExecutor(
        store.sessions,
        dispatcher,
        expert_reasoner=_CrashingExpertReasoner(),
    )
    handler = _StepThenFailHandler(
        BoundedV2Planner(runtime_mode="off"),
        operation_executor,
    )
    outcome = await DurableReadTurnExecutor(store.sessions, handler).execute(
        _request(
            "conv_runtime_expert_crash",
            "turn_runtime_expert_crash",
            "client-runtime-expert-crash",
            "worker-runtime-expert-crash",
        ),
        context,
    )
    assert outcome.status == TurnStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "expert_reasoning_failed"
    assert dispatcher.calls == 1
    with store.sessions() as session:
        rows = tuple(
            session.scalars(
                select(V2StepResult).where(
                    V2StepResult.turn_id == "turn_runtime_expert_crash"
                )
            )
        )
    assert len(rows) == 1
    persisted = ExpertResult.model_validate(rows[0].result)
    assert persisted.status == TaskStatus.PARTIAL_SUCCESS
    assert persisted.output == ProductResult(products=()).model_dump(mode="json")
    assert persisted.error is not None
    assert persisted.error.code == "expert_reasoning_failed"


@pytest.mark.asyncio
async def test_cancellation_survives_stale_lease_cleanup_conflicts(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_cancel_race")
    reasoner = _BlockingExpertReasoner()
    operation_executor = DurableOperationExecutor(
        store.sessions,
        _CatalogDispatcher(),
        expert_reasoner=reasoner,
    )
    runner = DurableReadTurnExecutor(
        store.sessions,
        _StepThenFailHandler(
            BoundedV2Planner(runtime_mode="off"),
            operation_executor,
        ),
    )
    task = asyncio.create_task(
        runner.execute(
            _request(
                "conv_runtime_cancel_race",
                "turn_runtime_cancel_race",
                "client-runtime-cancel-race",
                "worker-runtime-cancel-race",
            ),
            context,
        )
    )
    await asyncio.wait_for(reasoner.entered.wait(), timeout=5)
    with store.sessions() as session:
        session.execute(
            update(V2Turn)
            .where(V2Turn.id == "turn_runtime_cancel_race")
            .values(
                lease_owner="worker-runtime-stolen",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            )
        )
        session.commit()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with store.sessions() as session:
        turn = session.get(V2Turn, "turn_runtime_cancel_race")
        steps = tuple(
            session.scalars(
                select(V2StepResult).where(
                    V2StepResult.turn_id == "turn_runtime_cancel_race"
                )
            )
        )
    assert turn is not None
    assert turn.execution_state == TurnStatus.RUNNING.value
    assert turn.lease_owner == "worker-runtime-stolen"
    assert steps == ()


@pytest.mark.asyncio
async def test_cancellation_survives_unexpected_terminal_cleanup_failure(
    postgres_runtime_store: _RuntimeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = postgres_runtime_store
    context = _context()
    conversation_id = "conv_runtime_cancel_cleanup"
    turn_id = "turn_runtime_cancel_cleanup"
    lease_owner = "worker-runtime-cancel-cleanup"
    _conversation(store.sessions, context, conversation_id)
    handler = _BlockingHandler()
    runner = DurableReadTurnExecutor(store.sessions, handler)
    task = asyncio.create_task(
        runner.execute(
            _request(
                conversation_id,
                turn_id,
                "client-runtime-cancel-cleanup",
                lease_owner,
            ),
            context,
        )
    )
    await asyncio.wait_for(handler.entered.wait(), timeout=5)

    def fail_terminal_cleanup(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("synthetic terminal cleanup failure")

    monkeypatch.setattr(V2Repository, "complete_turn", fail_terminal_cleanup)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with store.sessions() as session:
        turn = session.get(V2Turn, turn_id)
    assert turn is not None
    assert turn.execution_state == TurnStatus.RUNNING.value
    assert turn.lease_owner == lease_owner


class _StepThenFailHandler:
    def __init__(
        self,
        planner: BoundedV2Planner,
        executor: DurableOperationExecutor,
    ) -> None:
        self.planner = planner
        self.executor = executor

    async def run_claimed(self, **kwargs: Any) -> TurnComputation:
        planned = await self.planner.plan(kwargs["message"], kwargs["context"])
        await self.executor.execute(
            turn_id=kwargs["turn_id"],
            lease_owner=kwargs["lease_owner"],
            access=kwargs["context"].access,
            operations=planned.initial_operations,
            plan_revision=0,
        )
        raise DurableExecutionError("synthetic_after_durable_step")


@pytest.mark.asyncio
async def test_completed_step_survives_terminal_failure_and_restart(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_partial")
    dispatcher = _CatalogDispatcher()
    operation_executor = DurableOperationExecutor(store.sessions, dispatcher)
    handler = _StepThenFailHandler(
        BoundedV2Planner(runtime_mode="off"), operation_executor
    )
    runner = DurableReadTurnExecutor(store.sessions, handler)
    request = _request(
        "conv_runtime_partial",
        "turn_runtime_partial",
        "client-runtime-partial",
        "worker-runtime-partial",
    )
    first = await runner.execute(request, context)
    retry = await runner.execute(
        request.model_copy(
            update={
                "turn_id": "turn_runtime_partial_retry",
                "lease_owner": "worker-runtime-partial-retry",
            }
        ),
        context,
    )
    assert first.status == retry.status == TurnStatus.FAILED
    assert dispatcher.calls == 1
    with store.sessions() as session:
        rows = tuple(
            session.scalars(
                select(V2StepResult).where(
                    V2StepResult.turn_id == "turn_runtime_partial"
                )
            )
        )
    assert len(rows) == 1 and rows[0].status == TaskStatus.SUCCESS.value


class _KnowledgeFailureDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, operation: RuntimeOperation, access: object
    ) -> ExpertResult:
        del operation, access
        self.calls += 1
        raise ModelRuntimeError("model_timeout", _model_failure_metadata())


@pytest.mark.asyncio
async def test_failed_knowledge_attempt_counter_survives_restart(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_knowledge")
    dispatcher = _KnowledgeFailureDispatcher()
    operation_executor = DurableOperationExecutor(store.sessions, dispatcher)
    handler = _StepThenFailHandler(
        BoundedV2Planner(runtime_mode="off"), operation_executor
    )
    runner = DurableReadTurnExecutor(store.sessions, handler)
    request = _request(
        "conv_runtime_knowledge",
        "turn_runtime_knowledge",
        "client-runtime-knowledge",
        "worker-runtime-knowledge",
        message="Sách này nói về chủ đề gì?",
    )
    first = await runner.execute(request, context)
    retry = await runner.execute(
        request.model_copy(
            update={
                "turn_id": "turn_runtime_knowledge_retry",
                "lease_owner": "worker-runtime-knowledge-retry",
            }
        ),
        context,
    )
    assert first.status == retry.status == TurnStatus.FAILED
    assert first.usage.knowledge_retrievals == retry.usage.knowledge_retrievals == 1
    assert dispatcher.calls == 1


class _RepairThenFailHandler:
    def __init__(self, executor: DurableOperationExecutor) -> None:
        self.executor = executor
        self.calls = 0

    async def run_claimed(self, **kwargs: Any) -> TurnComputation:
        self.calls += 1
        self.executor.checkpoint_draft_repair(
            turn_id=kwargs["turn_id"],
            lease_owner=kwargs["lease_owner"],
            access=kwargs["context"].access,
        )
        raise ModelRuntimeError("model_timeout", _model_failure_metadata())


@pytest.mark.asyncio
async def test_failed_repair_attempt_counter_survives_restart(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    context = _context()
    _conversation(store.sessions, context, "conv_runtime_repair")
    operation_executor = DurableOperationExecutor(store.sessions, _CatalogDispatcher())
    handler = _RepairThenFailHandler(operation_executor)
    runner = DurableReadTurnExecutor(
        store.sessions,
        handler,
        budget_ledger=store.ledger,
    )
    request = _request(
        "conv_runtime_repair",
        "turn_runtime_repair",
        "client-runtime-repair",
        "worker-runtime-repair",
    )
    first = await runner.execute(
        request,
        context,
        provider_budget=_budget(store, request.turn_id),
    )
    retry = await runner.execute(
        request.model_copy(
            update={
                "turn_id": "turn_runtime_repair_retry",
                "lease_owner": "worker-runtime-repair-retry",
            }
        ),
        context,
    )
    assert first.status == retry.status == TurnStatus.FAILED
    assert first.usage.draft_repairs == retry.usage.draft_repairs == 1
    assert handler.calls == 1


class _PlanningContextHandler:
    def __init__(self) -> None:
        self.calls = 0
        self.context: PlanningContext | None = None
        self.comparison_product_ids: tuple[int, ...] = ()

    async def run_claimed(self, **kwargs: Any) -> TurnComputation:
        self.calls += 1
        context: PlanningContext = kwargs["context"]
        self.context = context
        planned = await BoundedV2Planner(runtime_mode="off").plan(
            kwargs["message"], context
        )
        comparison = next(
            operation
            for operation in planned.initial_operations
            if operation.capability == "product.compare"
        )
        raw_product_ids = comparison.parameters["product_ids"]
        assert isinstance(raw_product_ids, list)
        self.comparison_product_ids = tuple(
            cast(int, product_id) for product_id in raw_product_ids
        )
        return _answered("History-aware follow-up")


@pytest.mark.asyncio
async def test_checkout_proposal_card_is_stored_and_replayed_without_recreation(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    access = tool_access(scopes=frozenset({"ecommerce.read", "ecommerce.write"}))
    context = PlanningContext(
        access=access,
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_runtime_action",
            corpus_version_id="corpus_runtime_a",
            index_manifest_id="index_runtime_a",
        ),
    )
    conversation_id = "conv_runtime_checkout_proposal"
    _conversation(store.sessions, context, conversation_id)
    _seed_checkout_cart(store.sessions, context)
    planner = _CountingPlanner()
    dispatcher = _CatalogDispatcher()
    producer = _NeverAnswerProducer()
    action_service = _CountingActionService(
        store.sessions,
        catalog_version_id=context.versions.catalog_version_id,
    )
    supervisor = V2ReadSupervisor(
        store.sessions,
        planner=planner,
        operation_executor=DurableOperationExecutor(store.sessions, dispatcher),
        answer_producer=producer,  # type: ignore[arg-type]
        action_service=action_service,
    )
    runner = DurableReadTurnExecutor(store.sessions, supervisor)
    request = _request(
        conversation_id,
        "turn_runtime_checkout_proposal",
        "client-runtime-checkout-proposal",
        "worker-runtime-checkout-proposal",
        message="Thanh toán giỏ hàng",
    )

    first = await runner.execute(request, context)
    retry = await runner.execute(
        request.model_copy(
            update={
                "turn_id": "turn_runtime_checkout_proposal_retry",
                "lease_owner": "worker-runtime-checkout-proposal-retry",
            }
        ),
        context,
    )

    assert first.status == retry.status == TurnStatus.COMPLETED
    assert first.outcome == retry.outcome == DialogueOutcome.AWAITING_CONFIRMATION
    assert first.result is not None
    assert first.result == retry.result
    assert retry.reused is True
    assert len(first.result.action_cards) == 1
    assert planner.calls == 1
    assert action_service.proposal_calls == 1
    assert dispatcher.calls == 0
    assert producer.calls == 0
    with store.sessions() as session:
        proposal_count = session.scalar(
            select(func.count())
            .select_from(V2Proposal)
            .where(V2Proposal.turn_id == first.turn_id)
        )
    assert proposal_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["confirm", "reject"])
async def test_expired_running_proposal_recovers_exact_completed_result_without_work(
    postgres_runtime_store: _RuntimeStore,
    decision: str,
) -> None:
    store = postgres_runtime_store
    suffix = f"_recovery_{decision}"
    access = ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id=f"tenant_runtime_recovery_{decision}",
            principal_id=f"principal_runtime_recovery_{decision}",
            mode=ConversationMode.SHOPPER,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read", "ecommerce.write"}),
    )
    original_context = PlanningContext(
        access=access,
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_runtime_action",
            corpus_version_id="corpus_runtime_a",
            index_manifest_id="index_runtime_a",
        ),
    )
    retry_context = original_context.model_copy(
        update={
            "versions": RuntimeDataVersions(
                catalog_version_id="catalog_runtime_action",
                corpus_version_id="corpus_runtime_b",
                index_manifest_id="index_runtime_b",
            )
        }
    )
    conversation_id = f"conv_runtime_proposal_recovery_{decision}"
    turn_id = f"turn_runtime_proposal_recovery_{decision}"
    client_turn_id = f"client-runtime-proposal-recovery-{decision}"
    lease_owner = f"worker-runtime-proposal-crashed-{decision}"
    _conversation(store.sessions, original_context, conversation_id)
    cart_id = _seed_checkout_cart(
        store.sessions,
        original_context,
        suffix=suffix,
    )
    _record_bind_claim(
        store.sessions,
        original_context,
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        lease_owner=lease_owner,
        message="Thanh toán giỏ hàng",
    )
    seed_actions = V2ActionService(
        store.sessions,
        catalog_version_id=original_context.versions.catalog_version_id,
    )
    proposal = seed_actions.propose_checkout(
        _authorization(original_context),
        conversation_id=conversation_id,
        turn_id=turn_id,
        lease_owner=lease_owner,
        request=CheckoutInput(cart_id=cart_id, expected_version=1),
    )
    with store.sessions.begin() as session:
        session.execute(
            update(V2Turn)
            .where(V2Turn.id == turn_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    planner = _CountingPlanner()
    dispatcher = _CatalogDispatcher()
    producer = _NeverAnswerProducer()
    recovery_actions = _CountingActionService(
        store.sessions,
        catalog_version_id=original_context.versions.catalog_version_id,
    )
    supervisor = V2ReadSupervisor(
        store.sessions,
        planner=planner,
        operation_executor=DurableOperationExecutor(store.sessions, dispatcher),
        answer_producer=producer,  # type: ignore[arg-type]
        action_service=recovery_actions,
    )
    foreign_accesses = (
        access.model_copy(
            update={
                "binding": access.binding.model_copy(
                    update={"principal_id": "principal_runtime_recovery_foreign"}
                )
            }
        ),
        access.model_copy(
            update={
                "binding": access.binding.model_copy(
                    update={"tenant_id": "tenant_runtime_recovery_foreign"}
                )
            }
        ),
    )
    for foreign_access in foreign_accesses:
        assert (
            supervisor.read_turn_proposal(
                conversation_id=conversation_id,
                turn_id=turn_id,
                access=foreign_access,
            )
            is None
        )
        assert (
            supervisor.recover_expired_turn_proposal(
                conversation_id=conversation_id,
                turn_id=turn_id,
                access=foreign_access,
            )
            is False
        )
    runner = DurableReadTurnExecutor(store.sessions, supervisor)
    request = _request(
        conversation_id,
        f"turn_runtime_proposal_recovery_retry_{decision}",
        client_turn_id,
        f"worker-runtime-proposal-recovery-{decision}",
        message="Thanh toán giỏ hàng",
    )
    recovered = await runner.execute(request, retry_context)
    replay = await runner.execute(
        request.model_copy(
            update={
                "turn_id": f"turn_runtime_proposal_recovery_replay_{decision}",
                "lease_owner": f"worker-runtime-proposal-replay-{decision}",
            }
        ),
        retry_context,
    )

    assert recovered.status == replay.status == TurnStatus.COMPLETED
    assert recovered.outcome == DialogueOutcome.AWAITING_CONFIRMATION
    assert recovered.reused is True and replay.reused is True
    assert recovered.result == replay.result
    assert recovered.result is not None
    assert recovered.result.action_cards == (proposal.action,)
    assert recovered.result.claims == ()
    assert recovered.result.citations == ()
    assert recovered.result.evidence == ()
    assert recovered.result.plan is None
    assert planner.calls == 0
    assert recovery_actions.proposal_calls == 0
    assert dispatcher.calls == 0
    assert producer.calls == 0
    with store.sessions() as session:
        turn = session.get(V2Turn, turn_id)
        assert turn is not None and turn.result is not None
        recovered_payload = dict(turn.result)
        runtime = cast(dict[str, object], turn.result["runtime"])
        assert runtime["data_versions"] == original_context.versions.model_dump(
            mode="json"
        )

    with pytest.raises(TurnStateConflictError):
        with store.sessions() as session:
            V2Repository(session).complete_turn(
                _authorization(original_context),
                turn_id,
                status=TurnStatus.COMPLETED,
                dialogue_outcome=DialogueOutcome.ANSWERED,
                result={"stale_worker": True},
                lease_owner=lease_owner,
                expected_status=TurnStatus.RUNNING,
            )
    with store.sessions() as session:
        turn = session.get(V2Turn, turn_id)
        assert turn is not None and turn.result == recovered_payload

    if decision == "confirm":
        first_decision = recovery_actions.confirm_action(
            _authorization(original_context),
            action_id=proposal.action.action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key=f"recovery-confirm-{decision}",
        )
        decision_replay = recovery_actions.confirm_action(
            _authorization(original_context),
            action_id=proposal.action.action_id,
            request=ActionConfirmRequest(proposal_version=1),
            idempotency_key=f"recovery-confirm-{decision}",
        )
        assert first_decision.status == ActionStatus.EXECUTED
        assert decision_replay == first_decision
    else:
        first_decision = recovery_actions.reject_action(
            _authorization(original_context),
            action_id=proposal.action.action_id,
            request=ActionRejectRequest(proposal_version=1, reason="user declined"),
        )
        decision_replay = recovery_actions.reject_action(
            _authorization(original_context),
            action_id=proposal.action.action_id,
            request=ActionRejectRequest(proposal_version=1, reason="user declined"),
        )
        assert first_decision.status == ActionStatus.REJECTED
        assert decision_replay == first_decision
    with store.sessions() as session:
        cart = session.get(V2Cart, cart_id)
        assert cart is not None
        assert cart.status == ("checked_out" if decision == "confirm" else "active")
        assert session.scalar(
            select(func.count()).select_from(V2Order).where(V2Order.cart_id == cart_id)
        ) == (1 if decision == "confirm" else 0)


@pytest.mark.asyncio
async def test_restarted_executor_loads_grounded_history_and_preferences(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    tools = read_tools(store.sessions)
    access = tool_access(
        scopes=frozenset({"ecommerce.read", "ecommerce.write", "merchant.read"})
    )
    context = PlanningContext(
        access=access,
        versions=RuntimeDataVersions(
            catalog_version_id=tools.catalog_snapshot.version_id,
            corpus_version_id="corpus_runtime_a",
            index_manifest_id="index_runtime_a",
        ),
    )
    conversation_id = "conv_runtime_history_followup"
    _conversation(store.sessions, context, conversation_id)
    supervisor = V2ReadSupervisor(
        store.sessions,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=DurableOperationExecutor(store.sessions, tools),
        answer_producer=GroundedAnswerProducer(  # type: ignore[arg-type]
            None, runtime_mode="off", model=None
        ),
    )
    first = await DurableReadTurnExecutor(store.sessions, supervisor).execute(
        _request(
            conversation_id,
            "turn_runtime_history_source",
            "client-runtime-history-source",
            "worker-runtime-history-source",
            message="Tìm 2 cuốn sách dưới 200 nghìn",
        ),
        context,
    )
    assert first.status == TurnStatus.COMPLETED
    with store.sessions() as session:
        V2HistoryService(session).put_preference(
            _authorization(context),
            PreferencePutRequest(
                source_turn_id=first.turn_id,
                preference=BudgetPreference(
                    kind=PreferenceKind.MAX_BUDGET_VND,
                    value=175_000,
                ),
            ),
        )

    handler = _PlanningContextHandler()
    follow_up = await DurableReadTurnExecutor(store.sessions, handler).execute(
        _request(
            conversation_id,
            "turn_runtime_history_followup",
            "client-runtime-history-followup",
            "worker-runtime-history-followup",
            message="So sánh chúng",
        ),
        context,
    )
    assert follow_up.status == TurnStatus.COMPLETED
    assert handler.calls == 1
    assert handler.context is not None
    assert handler.context.model_context.recent_turns[-1].user_message == (
        "Tìm 2 cuốn sách dưới 200 nghìn"
    )
    assert handler.context.model_context.preferences[0].preference.value == 175_000
    assert handler.context.model_context.referenced_product_ids
    assert (
        handler.comparison_product_ids
        == (handler.context.model_context.referenced_product_ids[:5])
    )
    constraints = {
        item.key: item.value
        for item in handler.context.model_context.active_constraints
    }
    assert constraints["max_budget_vnd"] == 175_000
    assert constraints["max_price_vnd"] == 200_000


@pytest.mark.asyncio
async def test_actual_no_context_catalog_read_is_grounded_and_persisted(
    postgres_runtime_store: _RuntimeStore,
) -> None:
    store = postgres_runtime_store
    tools = read_tools(store.sessions)
    access = tool_access()
    context = PlanningContext(
        access=access,
        versions=RuntimeDataVersions(
            catalog_version_id=tools.catalog_snapshot.version_id,
            corpus_version_id="corpus_runtime_a",
            index_manifest_id="index_runtime_a",
        ),
    )
    _conversation(store.sessions, context, "conv_runtime_e2e")
    operation_executor = DurableOperationExecutor(store.sessions, tools)
    supervisor = V2ReadSupervisor(
        store.sessions,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=operation_executor,
        answer_producer=GroundedAnswerProducer(  # type: ignore[arg-type]
            None,
            runtime_mode="off",
            model=None,
        ),
    )
    outcome = await DurableReadTurnExecutor(store.sessions, supervisor).execute(
        _request(
            "conv_runtime_e2e",
            "turn_runtime_e2e",
            "client-runtime-e2e",
            "worker-runtime-e2e",
            message="Tìm sách 'Sách thử nghiệm 1'",
        ),
        context,
    )
    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.outcome == DialogueOutcome.ANSWERED
    assert outcome.result is not None
    assert "Sách thử nghiệm 1" in outcome.result.answer
    assert outcome.result.claims and outcome.result.evidence


class _NeverHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def run_claimed(self, **_: Any) -> TurnComputation:
        self.calls += 1
        raise AssertionError("handler must not run")


class _RecoveryTrackingNeverHandler(_NeverHandler):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_calls = 0

    def recover_expired_turn_proposal(self, **_: Any) -> bool:
        self.recovery_calls += 1
        raise AssertionError("active running retry must not enter proposal recovery")


class _RenewLeaseBeforeInterruptExecutor(DurableReadTurnExecutor):
    def _persist_error(
        self,
        turn_id: str,
        access: ResourceAuthorization,
        *,
        status: TurnStatus,
        error: SafeExecutionError,
        provider_budget: ProviderBudgetContext | None,
        lease_owner: str | None = None,
        expected_status: TurnStatus | None = None,
        require_expired_lease: bool = False,
    ) -> DurableTurnOutcome:
        with self.session_factory() as session, session.begin():
            session.execute(
                text(
                    "UPDATE v2_turns SET lease_owner = 'worker-runtime-renewed', "
                    "lease_expires_at = clock_timestamp() + interval '5 minutes' "
                    "WHERE id = :turn_id"
                ),
                {"turn_id": turn_id},
            )
        return super()._persist_error(
            turn_id,
            access,
            status=status,
            error=error,
            provider_budget=provider_budget,
            lease_owner=lease_owner,
            expected_status=expected_status,
            require_expired_lease=require_expired_lease,
        )


def _context(
    *,
    corpus: str = "corpus_runtime_a",
    index: str = "index_runtime_a",
) -> PlanningContext:
    return PlanningContext(
        access=tool_access(),
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_runtime_a",
            corpus_version_id=corpus,
            index_manifest_id=index,
        ),
    )


def _authorization(context: PlanningContext) -> AuthorizationContext:
    return AuthorizationContext(
        principal_id=context.access.binding.principal_id,
        tenant_id=context.access.binding.tenant_id,
        scopes=context.access.scopes,
    )


def _conversation(
    sessions: sessionmaker[Session],
    context: PlanningContext,
    conversation_id: str,
) -> None:
    with sessions() as session:
        V2Repository(session).create_conversation(
            _authorization(context),
            conversation_id=conversation_id,
            mode=context.access.binding.mode,
        )


def _seed_checkout_cart(
    sessions: sessionmaker[Session],
    context: PlanningContext,
    *,
    suffix: str = "",
) -> str:
    binding = context.access.binding
    product_id = 1_987_654_321 + sum(ord(char) for char in suffix)
    offer_id = f"offer_runtime_checkout{suffix}"
    cart_id = f"cart_runtime_checkout{suffix}"
    line_id = f"line_runtime_checkout{suffix}"
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO products "
                "(id, name, category, price, description, platform) VALUES "
                "(:product, 'Runtime checkout book', 'Books', 100000, "
                "'Runtime proposal fixture', 'Tiki')"
            ),
            {"product": product_id},
        )
        session.execute(
            text(
                "INSERT INTO v2_offers "
                "(id, tenant_id, store_id, product_id, demo_price_vnd, stock, "
                "version) VALUES "
                "(:offer, :tenant, :store, :product, "
                "100000, 10, 1)"
            ),
            {
                "offer": offer_id,
                "tenant": binding.tenant_id,
                "store": binding.store_id,
                "product": product_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO v2_carts "
                "(id, tenant_id, principal_id, store_id, status, version) VALUES "
                "(:cart, :tenant, :principal, :store, "
                "'active', 1)"
            ),
            {
                "cart": cart_id,
                "tenant": binding.tenant_id,
                "principal": binding.principal_id,
                "store": binding.store_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO v2_cart_lines "
                "(id, cart_id, tenant_id, principal_id, store_id, offer_id, "
                "quantity, offer_version) VALUES "
                "(:line, :cart, :tenant, :principal, :store, :offer, 2, 1)"
            ),
            {
                "line": line_id,
                "cart": cart_id,
                "tenant": binding.tenant_id,
                "principal": binding.principal_id,
                "store": binding.store_id,
                "offer": offer_id,
            },
        )
    return cart_id


def _request(
    conversation_id: str,
    turn_id: str,
    client_turn_id: str,
    lease_owner: str,
    *,
    message: str = "Tìm sách Sapiens",
) -> DurableTurnRequest:
    return DurableTurnRequest(
        conversation_id=conversation_id,
        turn_id=turn_id,
        client_turn_id=client_turn_id,
        message=message,
        lease_owner=lease_owner,
    )


def _stable_payload(
    conversation_id: str,
    client_turn_id: str,
    message: str = "Tìm sách Sapiens",
) -> dict[str, object]:
    return {
        "conversation_id": conversation_id,
        "client_turn_id": client_turn_id,
        "message": message,
    }


def _record_bind_claim(
    sessions: sessionmaker[Session],
    context: PlanningContext,
    *,
    conversation_id: str,
    turn_id: str,
    client_turn_id: str,
    lease_owner: str,
    message: str = "Tìm sách Sapiens",
) -> None:
    with sessions() as session:
        repository = V2Repository(session)
        repository.record_turn(
            _authorization(context),
            conversation_id=conversation_id,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            payload=_stable_payload(conversation_id, client_turn_id, message),
            corpus_version_id=context.versions.corpus_version_id,
        )
        repository.bind_turn_runtime(
            _authorization(context),
            turn_id,
            data_versions=context.versions.model_dump(mode="json"),
        )
        repository.claim_turn(
            _authorization(context),
            turn_id,
            lease_owner=lease_owner,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )


def _budget(store: _RuntimeStore, scope_id: str) -> ProviderBudgetContext:
    store.ledger.create_scope(
        scope_id=scope_id,
        account_id="p4-runtime-account",
        purpose="chat",
    )
    return ProviderBudgetContext(
        ledger=store.ledger,
        scope_id=scope_id,
        purpose="chat",
    )


def _answered(text: str) -> TurnComputation:
    return TurnComputation(
        result=TurnResult(outcome=DialogueOutcome.ANSWERED, answer=text)
    )


def _model_failure_metadata() -> ModelCallMetadata:
    return ModelCallMetadata(
        call_id="mcall_runtime_failure_00000000000001",
        stage="runtime.synthetic",
        agent_id="runtime_test",
        model="test-model",
        status="failed",
        duration_ms=max(0.0, monotonic() * 0),
        attempts=1,
        error_code="model_timeout",
    )
