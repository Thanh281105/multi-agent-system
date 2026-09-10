"""Real PostgreSQL gate for claimed, restart-safe v2 read execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext, TaskStatus
from app.db.migrate import upgrade_database
from app.db.v2_repository import V2Repository
from app.models.v2 import V2KnowledgeCorpusVersion, V2StepResult, V2Turn
from app.shared import ModelCallMetadata, ModelRuntimeError
from app.shared.budget import (
    PricingManifest,
    ProviderBudgetContext,
    ProviderUsage,
    SQLProviderBudgetLedger,
    current_provider_budget,
    default_pricing_manifest_path,
)
from app.v2.answers import GroundedAnswerProducer
from app.v2.contracts import DialogueOutcome, TurnResult, TurnStatus
from app.v2.execution import (
    DurableExecutionError,
    DurableOperationExecutor,
    DurableReadTurnExecutor,
    DurableTurnRequest,
    TurnComputation,
)
from app.v2.planning import (
    BoundedV2Planner,
    PlanningContext,
    RuntimeDataVersions,
)
from app.v2.registry import ProductResult
from app.v2.runtime_contracts import ExpertResult, RuntimeOperation, ToolEvidence
from app.v2.supervisor import V2ReadSupervisor
from tests.test_v2_tools import read_tools, seed_tool_catalog, tool_access
from tests.v2_postgres_support import disposable_postgres_database


@dataclass(frozen=True, slots=True)
class _RuntimeStore:
    sessions: sessionmaker[Session]
    ledger: SQLProviderBudgetLedger


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
async def test_expired_running_turn_is_interrupted_and_never_reclaimed(
    postgres_runtime_store: _RuntimeStore,
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
        answer_producer=GroundedAnswerProducer(
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
) -> None:
    with sessions() as session:
        repository = V2Repository(session)
        repository.record_turn(
            _authorization(context),
            conversation_id=conversation_id,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            payload=_stable_payload(conversation_id, client_turn_id),
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
