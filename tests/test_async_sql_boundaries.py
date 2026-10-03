"""Independent heartbeat and thread-affinity regressions for synchronous SQL."""

import asyncio
from collections import Counter
from threading import Event, get_ident
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import create_database_engine
from app.db.v2_repository import V2Repository
from app.models.budget import ProviderBudgetScope
from app.shared.budget import provider_budget_scope
from app.shared.model_runtime import OpenAIModelRuntime
from app.v2.contracts import TurnStatus
from app.v2.execution import DurableReadTurnExecutor, DurableTurnRequest
from app.v2.turn_service import V2TurnService
from tests.test_budget_runtime import (
    Answer,
    FakeGenerationClient,
    add_scope,
    generation_response,
    read_attempts,
)
from tests.test_budget_runtime import (
    budget_store as budget_store,
)
from tests.test_v2_turn_service import (
    _answered,
    _chat,
    _context,
    _conversation,
    _Store,
)
from tests.test_v2_turn_service import (
    postgres_store as postgres_store,
)
from tests.v2_postgres_support import disposable_postgres_database


async def heartbeat_during_sql(started: Event, task: asyncio.Task[Any]) -> None:
    # The SQL worker waits for the test to release it. An independent coroutine
    # must keep advancing while that transaction is still unfinished.
    await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=3)
    pulses = 0
    for _ in range(3):
        await asyncio.sleep(0.01)
        assert not task.done()
        pulses += 1
    assert pulses == 3


class ReadHandler:
    def __init__(self, *, blocked: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.calls = 0

    async def run_claimed(self, **_: Any) -> Any:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return _answered("SQL worker regression")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["record_turn", "bind_turn_runtime", "claim_turn", "complete_turn"]
)
async def test_slow_durable_phase_keeps_loop_live_and_sessions_in_worker(
    postgres_store: _Store,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    loop_thread = get_ident()
    started, release = Event(), Event()
    sessions: list[tuple[str, int]] = []

    def session_factory() -> Session:
        created_thread = get_ident()
        assert created_thread != loop_thread
        session = postgres_store.sessions()
        sessions.append(("open", created_thread))
        original_close = session.close

        def close() -> None:
            assert get_ident() == created_thread
            sessions.append(("close", created_thread))
            original_close()

        monkeypatch.setattr(session, "close", close)
        return session

    original = getattr(V2Repository, phase)

    def delayed(*args: Any, **kwargs: Any) -> Any:
        assert get_ident() != loop_thread
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    context, request = _context(f"slow_{phase}"), _chat(f"slow_{phase}")
    _conversation(postgres_store.sessions, context, request.conversation_id)
    monkeypatch.setattr(V2Repository, phase, delayed)
    service = V2TurnService(DurableReadTurnExecutor(session_factory, ReadHandler()))
    progress: list[str] = []

    async def capture(event: Any) -> None:
        assert get_ident() == loop_thread
        progress.append(event.phase.value)

    task = asyncio.create_task(service.execute(request, context, progress=capture))
    try:
        await heartbeat_during_sql(started, task)
    finally:
        release.set()
    outcome = await asyncio.wait_for(task, timeout=5)
    assert outcome.status == TurnStatus.COMPLETED
    assert progress == ["admitted", "claimed", "terminal"]
    assert Counter(
        thread for action, thread in sessions if action == "open"
    ) == Counter(thread for action, thread in sessions if action == "close")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slow_cancel_is_durable_without_blocking_loop(
    postgres_store: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = get_ident()
    context, request = _context("slow_cancel_sql"), _chat("slow_cancel_sql")
    _conversation(postgres_store.sessions, context, request.conversation_id)
    handler = ReadHandler(blocked=True)
    service = V2TurnService(DurableReadTurnExecutor(postgres_store.sessions, handler))
    execution = asyncio.create_task(service.execute(request, context))
    await asyncio.wait_for(handler.entered.wait(), timeout=5)
    started, release = Event(), Event()
    original = V2Repository.cancel_turn

    def delayed(*args: Any, **kwargs: Any) -> Any:
        assert get_ident() != loop_thread
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(V2Repository, "cancel_turn", delayed)
    from app.db.v2_repository import canonical_turn_id

    turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
    cancellation = asyncio.create_task(service.cancel(turn_id, context.access))
    try:
        await heartbeat_during_sql(started, cancellation)
    finally:
        release.set()
    assert (
        await asyncio.wait_for(cancellation, timeout=5)
    ).status == TurnStatus.CANCELLED
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=5)
    assert (
        await asyncio.to_thread(service.query, turn_id, context.access)
    ).status == TurnStatus.CANCELLED


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["record_turn", "bind_turn_runtime", "claim_turn"])
async def test_cancelled_admission_fences_late_worker_claim(
    postgres_store: _Store,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    context, request = _context(f"abort_{phase}"), _chat(f"abort_{phase}")
    _conversation(postgres_store.sessions, context, request.conversation_id)
    started, release = Event(), Event()
    original = getattr(V2Repository, phase)

    def delayed(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(V2Repository, phase, delayed)
    handler = ReadHandler()
    executor = DurableReadTurnExecutor(postgres_store.sessions, handler)
    durable = DurableTurnRequest(
        conversation_id=request.conversation_id,
        turn_id=f"turn_abort_{phase}",
        client_turn_id=request.client_turn_id,
        message=request.message,
        lease_owner=f"worker_abort_{phase}",
    )
    task = asyncio.create_task(executor.admit(durable, context))
    await heartbeat_during_sql(started, task)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    outcome = await asyncio.to_thread(executor.query, durable.turn_id, context.access)
    assert outcome.status == TurnStatus.CANCELLED
    assert handler.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_flag", [False, True])
async def test_cancel_during_generation_reservation_closes_ledger_slot(
    budget_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    cancel_flag: bool,
) -> None:
    ledger, factory, _ = budget_store
    cancellation, started, release = Event(), Event(), Event()
    context = add_scope(
        ledger,
        f"reserve_abort_{cancel_flag}",
        cancellation_requested=cancellation.is_set,
    )
    original = ledger.reserve_attempt
    loop_thread = get_ident()

    def delayed(**kwargs: Any) -> Any:
        assert get_ident() != loop_thread
        result = original(**kwargs)
        started.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(ledger, "reserve_attempt", delayed)
    client = FakeGenerationClient([generation_response()])
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)

    async def generate() -> Any:
        with provider_budget_scope(context):
            return await runtime.generate_structured(
                stage="planning",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
            )

    task = asyncio.create_task(generate())
    await heartbeat_during_sql(started, task)
    if cancel_flag:
        cancellation.set()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert not client.responses.create_requests
    attempt = read_attempts(factory, context.scope_id)[0]
    assert attempt.result_status == "cancelled"
    assert attempt.usage_status == ("known" if cancel_flag else "unknown")
    if cancel_flag:
        assert attempt.total_tokens == 0 and attempt.transport_started_at is None
    with factory() as session:
        scope = session.get(ProviderBudgetScope, context.scope_id)
        assert scope is not None and scope.active_attempts == 0
        assert scope.reserved_cost_nano_usd == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancelled_retry_admission_keeps_the_original_worker_claim(
    postgres_store: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, request = _context("abort_retry"), _chat("abort_retry")
    _conversation(postgres_store.sessions, context, request.conversation_id)
    handler = ReadHandler(blocked=True)
    executor = DurableReadTurnExecutor(postgres_store.sessions, handler)
    service = V2TurnService(executor)
    original_task = asyncio.create_task(service.execute(request, context))
    await asyncio.wait_for(handler.entered.wait(), timeout=5)
    started, release = Event(), Event()
    original_record = V2Repository.record_turn

    def delayed(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(timeout=5)
        return original_record(*args, **kwargs)

    monkeypatch.setattr(V2Repository, "record_turn", delayed)
    retry = asyncio.create_task(service.execute(request, context))
    await heartbeat_during_sql(started, retry)
    retry.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(retry, timeout=5)
    from app.db.v2_repository import canonical_turn_id

    turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
    assert (
        await asyncio.to_thread(service.query, turn_id, context.access)
    ).status == TurnStatus.RUNNING
    handler.release.set()
    assert (
        await asyncio.wait_for(original_task, timeout=5)
    ).status == TurnStatus.COMPLETED
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_cancel_during_generation_settlement_finishes_usage_and_result(
    budget_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "settlement_abort")
    started, release = Event(), Event()
    original = ledger.settle_known_usage
    loop_thread = get_ident()

    def delayed(**kwargs: Any) -> Any:
        assert get_ident() != loop_thread
        result = original(**kwargs)
        started.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(ledger, "settle_known_usage", delayed)
    client = FakeGenerationClient([generation_response()])
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)

    async def generate() -> Any:
        with provider_budget_scope(context):
            return await runtime.generate_structured(
                stage="planning",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
            )

    task = asyncio.create_task(generate())
    await heartbeat_during_sql(started, task)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    attempt = read_attempts(factory, context.scope_id)[0]
    assert (attempt.usage_status, attempt.result_status) == ("known", "success")
    assert len(client.responses.create_requests) == 1
    with factory() as session:
        scope = session.get(ProviderBudgetScope, context.scope_id)
        assert scope is not None and scope.active_attempts == 0
        assert scope.reserved_cost_nano_usd == 0


@pytest.mark.integration
def test_postgres_engine_sets_bounded_timeouts() -> None:
    with disposable_postgres_database("thanh_v2_p2_sql_timeout_") as url:
        engine = create_database_engine(url)
        try:
            assert engine.pool.timeout() == 5  # type: ignore[attr-defined]
            with engine.connect() as connection:
                assert connection.scalar(text("SHOW statement_timeout")) == "10s"
                assert connection.scalar(text("SHOW lock_timeout")) == "5s"
        finally:
            engine.dispose()
