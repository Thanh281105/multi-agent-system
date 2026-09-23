"""Real PostgreSQL adversarial tests for canonical v2 turn execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext
from app.db.migrate import upgrade_database
from app.db.v2_repository import (
    TurnRuntimeConflictError,
    TurnStateConflictError,
    V2Repository,
    canonical_turn_id,
)
from app.models.v2 import V2KnowledgeCorpusVersion, V2Turn
from app.shared.budget import (
    PricingManifest,
    ProviderBudgetContext,
    ProviderUsage,
    SQLProviderBudgetLedger,
    default_pricing_manifest_path,
)
from app.v2.authorization import (
    ResourceAuthorization,
    ResourceNotFoundError,
    bind_request_authorization,
)
from app.v2.contracts import (
    ChatRequest,
    ConversationMode,
    DialogueOutcome,
    TurnResult,
    TurnStatus,
)
from app.v2.execution import (
    DurableReadTurnExecutor,
    DurableTurnRequest,
    TurnComputation,
)
from app.v2.planning import PlanningContext, RuntimeDataVersions
from app.v2.progress import TurnProgress, TurnProgressPhase
from app.v2.turn_service import V2TurnService
from tests.v2_postgres_support import disposable_postgres_database


@dataclass(frozen=True, slots=True)
class _Store:
    sessions: sessionmaker[Session]
    ledger: SQLProviderBudgetLedger


@pytest.fixture(scope="module")
def postgres_store() -> Iterator[_Store]:
    with disposable_postgres_database("thanh_v2_p2_turnsvc_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
        with sessions() as session:
            session.add(
                V2KnowledgeCorpusVersion(
                    id="corpus_turn_service",
                    corpus_name="turn-service-fixture",
                    version="v1",
                    status="published",
                    manifest={},
                    published_at=datetime.now(UTC),
                )
            )
            session.commit()
        ledger = SQLProviderBudgetLedger(
            sessions,
            PricingManifest.load(default_pricing_manifest_path()),
        )
        ledger.create_account(account_id="turn-service-account")
        try:
            yield _Store(sessions=sessions, ledger=ledger)
        finally:
            engine.dispose()


class _UsageHandler:
    def __init__(self, ledger: SQLProviderBudgetLedger, *, block: bool = False) -> None:
        self.ledger = ledger
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def run_claimed(self, **kwargs: Any) -> TurnComputation:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        scope_id = str(kwargs["turn_id"])
        reservation = self.ledger.reserve_attempt(
            scope_id=scope_id,
            call_id=f"mcall_{scope_id[5:37]}",
            attempt_number=1,
            operation="generation",
            purpose="chat",
            model="gpt-5.4-mini",
            request_fingerprint_sha256="c" * 64,
            input_token_bound=20,
            output_token_bound=4,
        )
        assert self.ledger.mark_dispatched(
            scope_id=scope_id,
            attempt_id=reservation.attempt_id,
        )
        self.ledger.settle_known_usage(
            scope_id=scope_id,
            attempt_id=reservation.attempt_id,
            usage=ProviderUsage(
                input_tokens=9,
                cached_input_tokens=2,
                output_tokens=3,
                reasoning_tokens=1,
                total_tokens=12,
            ),
            response_id=f"resp_{scope_id[5:37]}",
            response_model="gpt-5.4-mini-2026-03-17",
            response_service_tier="default",
        )
        self.ledger.record_attempt_result(
            scope_id=scope_id,
            attempt_id=reservation.attempt_id,
            result_status="success",
        )
        return _answered("canonical result")


class _NeverHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def run_claimed(self, **_: Any) -> TurnComputation:
        self.calls += 1
        raise AssertionError("a cancelled or replay caller must not run")


@pytest.mark.asyncio
async def test_same_client_retry_attaches_without_duplicate_usage(
    postgres_store: _Store,
) -> None:
    store = postgres_store
    context = _context("retry")
    request = _chat("retry")
    _conversation(store.sessions, context, request.conversation_id)
    turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
    budget = _budget(store, turn_id)
    handler = _UsageHandler(store.ledger, block=True)
    service = V2TurnService(
        DurableReadTurnExecutor(
            store.sessions,
            handler,
            budget_ledger=store.ledger,
        ),
        worker_id="worker_turn_service_retry",
    )
    progress: list[TurnProgress] = []

    async def capture(event: TurnProgress) -> None:
        progress.append(event)

    first_task = asyncio.create_task(
        service.execute(request, context, provider_budget=budget, progress=capture)
    )
    await asyncio.wait_for(handler.entered.wait(), timeout=5)
    retry_task = asyncio.create_task(
        service.execute(request, context, provider_budget=budget, progress=capture)
    )
    await asyncio.sleep(0)
    handler.release.set()
    first, retry = await asyncio.gather(first_task, retry_task)

    usage = store.ledger.scope_usage_summary(turn_id)
    assert first.status.value == retry.status.value == "completed"
    assert first.result == retry.result
    assert first.turn_id == retry.turn_id == turn_id
    assert retry.reused is True
    assert handler.calls == 1
    assert usage.provider_attempts == 1
    assert usage.input_tokens == 9
    assert usage.output_tokens == 3
    assert [event.phase for event in progress].count(TurnProgressPhase.CLAIMED) == 1
    assert [event.phase for event in progress].count(TurnProgressPhase.TERMINAL) == 2
    with store.sessions() as session:
        rows = tuple(
            session.scalars(
                select(V2Turn).where(V2Turn.conversation_id == request.conversation_id)
            )
        )
    assert len(rows) == 1
    assert rows[0].id == turn_id


@pytest.mark.asyncio
async def test_cancel_is_idempotent_owner_scoped_and_stops_stale_worker(
    postgres_store: _Store,
) -> None:
    store = postgres_store
    context = _context("cancel")
    request = _chat("cancel")
    _conversation(store.sessions, context, request.conversation_id)
    turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
    _budget(store, turn_id)
    handler = _UsageHandler(store.ledger, block=True)
    service = V2TurnService(
        DurableReadTurnExecutor(
            store.sessions,
            handler,
            budget_ledger=store.ledger,
        ),
        worker_id="worker_turn_service_cancel",
    )
    running = asyncio.create_task(service.execute(request, context))
    await asyncio.wait_for(handler.entered.wait(), timeout=5)

    foreign = _context("cancel", principal_id="principal_turn_service_foreign")
    with pytest.raises(ResourceNotFoundError):
        service.query(turn_id, foreign.access)
    with pytest.raises(ResourceNotFoundError):
        await service.cancel(turn_id, foreign.access)

    first = await service.cancel(turn_id, context.access)
    second = await service.cancel(turn_id, context.access)
    assert first.status.value == second.status.value == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await running
    assert handler.calls == 1
    assert store.ledger.scope_usage_summary(turn_id).provider_attempts == 0

    authorization = _authorization(context.access)
    with store.sessions() as session:
        repository = V2Repository(session)
        with pytest.raises(TurnRuntimeConflictError):
            repository.checkpoint_turn_runtime(
                authorization,
                turn_id,
                lease_owner="worker_turn_service_cancel:stale",
                knowledge_retrievals=1,
            )
        with pytest.raises(TurnStateConflictError):
            repository.complete_turn(
                authorization,
                turn_id,
                status=TurnStatus.COMPLETED,
                dialogue_outcome=DialogueOutcome.ANSWERED,
                result={"answer": "stale"},
                lease_owner="worker_turn_service_cancel:stale",
                expected_status=TurnStatus.RUNNING,
            )


@pytest.mark.asyncio
async def test_cancelled_admission_cannot_start_handler(
    postgres_store: _Store,
) -> None:
    store = postgres_store
    context = _context("pre_run_cancel")
    request = _chat("pre_run_cancel")
    _conversation(store.sessions, context, request.conversation_id)
    turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
    never = _NeverHandler()
    executor = DurableReadTurnExecutor(store.sessions, never)
    admission = await executor.admit(
        DurableTurnRequest(
            conversation_id=request.conversation_id,
            turn_id=turn_id,
            client_turn_id=request.client_turn_id,
            message=request.message,
            lease_owner="worker_pre_run_cancel",
        ),
        context,
    )
    assert admission.claimed is True
    with store.sessions() as session:
        V2Repository(session).cancel_turn(_authorization(context.access), turn_id)

    outcome = await executor.run_admitted(admission, context)
    assert outcome.status.value == "cancelled"
    assert never.calls == 0


def test_cancel_complete_race_has_one_terminal_winner(
    postgres_store: _Store,
) -> None:
    store = postgres_store
    context = _context("terminal_race")
    request = _chat("terminal_race")
    _conversation(store.sessions, context, request.conversation_id)
    turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
    authorization = _authorization(context.access)
    lease_owner = "worker_terminal_race"
    with store.sessions() as session:
        repository = V2Repository(session)
        repository.admit_turn(
            authorization,
            conversation_id=request.conversation_id,
            client_turn_id=request.client_turn_id,
            payload=request.model_dump(mode="json"),
            corpus_version_id=context.versions.corpus_version_id,
        )
        repository.claim_turn(
            authorization,
            turn_id,
            lease_owner=lease_owner,
            lease_duration=timedelta(seconds=30),
        )

    barrier = Barrier(2)

    def complete() -> str:
        barrier.wait(timeout=5)
        try:
            with store.sessions() as session:
                V2Repository(session).complete_turn(
                    authorization,
                    turn_id,
                    status=TurnStatus.COMPLETED,
                    dialogue_outcome=DialogueOutcome.ANSWERED,
                    result={"answer": "winner"},
                    lease_owner=lease_owner,
                    expected_status=TurnStatus.RUNNING,
                )
            return "completed"
        except TurnStateConflictError:
            return "cancelled"

    def cancel() -> str:
        barrier.wait(timeout=5)
        with store.sessions() as session:
            turn = V2Repository(session).cancel_turn(authorization, turn_id)
            return str(turn.execution_state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        complete_future = pool.submit(complete)
        cancel_future = pool.submit(cancel)
        observed = {
            complete_future.result(timeout=10),
            cancel_future.result(timeout=10),
        }

    assert observed in ({"completed"}, {"cancelled"})
    with store.sessions() as session:
        turn = V2Repository(session).get_turn(authorization, turn_id)
        assert turn.execution_state in {"completed", "cancelled"}
        if turn.execution_state == "completed":
            assert turn.result == {"answer": "winner"}
        else:
            assert turn.result is None


def _context(
    key: str,
    *,
    principal_id: str | None = None,
) -> PlanningContext:
    authorization = AuthorizationContext(
        tenant_id=f"tenant_turn_service_{key}",
        principal_id=principal_id or f"principal_turn_service_{key}",
        scopes=frozenset({"ecommerce.read"}),
    )
    return PlanningContext(
        access=bind_request_authorization(authorization, ConversationMode.SHOPPER),
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_turn_service",
            corpus_version_id="corpus_turn_service",
            index_manifest_id="index_turn_service",
        ),
    )


def _chat(key: str) -> ChatRequest:
    return ChatRequest(
        conversation_id=f"conv_turn_service_{key}",
        client_turn_id=f"client-turn-service-{key}",
        message="Tìm sách đã kiểm chứng",
    )


def _conversation(
    sessions: sessionmaker[Session],
    context: PlanningContext,
    conversation_id: str,
) -> None:
    with sessions() as session:
        V2Repository(session).create_conversation(
            _authorization(context.access),
            conversation_id=conversation_id,
            mode=context.access.binding.mode,
        )


def _authorization(access: ResourceAuthorization) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id=access.binding.tenant_id,
        principal_id=access.binding.principal_id,
        scopes=access.scopes,
    )


def _budget(store: _Store, turn_id: str) -> ProviderBudgetContext:
    store.ledger.create_scope(
        scope_id=turn_id,
        account_id="turn-service-account",
        purpose="chat",
    )
    return ProviderBudgetContext(
        ledger=store.ledger,
        scope_id=turn_id,
        purpose="chat",
    )


def _answered(text: str) -> TurnComputation:
    return TurnComputation(
        result=TurnResult(outcome=DialogueOutcome.ANSWERED, answer=text)
    )
