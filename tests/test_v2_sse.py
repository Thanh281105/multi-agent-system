"""Public v2 SSE serialization, ownership, and durable recovery tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext, TaskStatus
from app.db.migrate import upgrade_database
from app.db.v2_repository import V2Repository, canonical_turn_id
from app.gateway import v2_stream
from app.gateway.v2_stream import build_v2_streaming_response
from app.models.v2 import V2KnowledgeCorpusVersion
from app.shared.budget import (
    PricingManifest,
    ProviderBudgetContext,
    ProviderUsage,
    SQLProviderBudgetLedger,
    default_pricing_manifest_path,
)
from app.v2.authorization import ResourceAuthorization, bind_request_authorization
from app.v2.contracts import (
    ChatRequest,
    ConversationMode,
    DialogueOutcome,
    SafeExecutionError,
    TurnResult,
    TurnStatus,
)
from app.v2.execution import (
    DurableReadTurnExecutor,
    DurableTurnOutcome,
    TurnComputation,
)
from app.v2.planning import PlanningContext, RuntimeDataVersions
from app.v2.progress import (
    ProgressCallback,
    TurnProgress,
    TurnProgressHub,
    TurnProgressPhase,
)
from app.v2.runtime import ResolvedV2Runtime
from app.v2.turn_service import V2TurnService
from tests.v2_postgres_support import disposable_postgres_database


class _RequestProbe:
    def __init__(self, *, disconnect_on: int | None = None, suffix: str = "001"):
        self.state = SimpleNamespace(
            request_id=f"request_sse_{suffix}",
            trace_id=f"trace_sse_{suffix}",
        )
        self.disconnect_on = disconnect_on
        self.disconnect_checks = 0

    async def is_disconnected(self) -> bool:
        self.disconnect_checks += 1
        return (
            self.disconnect_on is not None
            and self.disconnect_checks >= self.disconnect_on
        )


class _ScriptedTurnService:
    def __init__(
        self,
        *,
        turn_id: str,
        outcome: DurableTurnOutcome,
        progress: tuple[TurnProgress, ...],
        block: bool = False,
        duplicate_terminal: bool = False,
        query_outcomes: tuple[DurableTurnOutcome, ...] = (),
        cancel_outcome: DurableTurnOutcome | None = None,
    ) -> None:
        self.turn_id = turn_id
        self.outcome = outcome
        self.progress = progress
        self.progress_hub = TurnProgressHub()
        self.execute_calls = 0
        self.query_calls = 0
        self.cancel_calls = 0
        self.execution_cancelled = False
        self.persisted = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.duplicate_terminal = duplicate_terminal
        self.query_outcomes = list(query_outcomes)
        self.cancel_outcome = cancel_outcome
        if not block:
            self.release.set()

    async def execute(
        self,
        _payload: ChatRequest,
        _context: object,
        *,
        provider_budget: ProviderBudgetContext | None = None,
        progress: ProgressCallback | None = None,
    ) -> DurableTurnOutcome:
        del provider_budget
        self.execute_calls += 1
        for event in self.progress:
            await self.progress_hub.publish(event)
            if progress is not None:
                await progress(event)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.execution_cancelled = True
            raise
        self.persisted = self.outcome.status in _TERMINAL
        if self.persisted:
            terminal = TurnProgress(
                turn_id=self.turn_id,
                phase=TurnProgressPhase.TERMINAL,
                turn_status=self.outcome.status,
            )
            await self.progress_hub.publish(terminal)
            if self.duplicate_terminal:
                await self.progress_hub.publish(terminal)
        self.finished.set()
        return self.outcome

    def query(
        self,
        _turn_id: str,
        _access: object,
        *,
        provider_budget: ProviderBudgetContext | None = None,
    ) -> DurableTurnOutcome:
        del provider_budget
        self.query_calls += 1
        if self.query_outcomes:
            return self.query_outcomes.pop(0)
        return self.outcome

    async def cancel(
        self,
        _turn_id: str,
        _access: object,
        *,
        provider_budget: ProviderBudgetContext | None = None,
    ) -> DurableTurnOutcome:
        del provider_budget
        self.cancel_calls += 1
        if self.cancel_outcome is None:
            raise AssertionError("this observer must never cancel its owner")
        self.persisted = True
        return self.cancel_outcome


class _LocalObserverService:
    def __init__(self, turn_id: str, completed: DurableTurnOutcome) -> None:
        self.progress_hub = TurnProgressHub()
        self.turn_id = turn_id
        self.completed = completed
        self.release = asyncio.Event()
        self.owner_finished = asyncio.Event()
        self.wrapper_cancelled = False
        self.owner_cancelled = False
        self.cancel_calls = 0
        self._owner = asyncio.create_task(self._run_owner())

    async def _run_owner(self) -> DurableTurnOutcome:
        try:
            await self.release.wait()
            return self.completed
        except asyncio.CancelledError:
            self.owner_cancelled = True
            raise
        finally:
            self.owner_finished.set()

    async def execute(
        self,
        _payload: ChatRequest,
        _context: object,
        *,
        provider_budget: ProviderBudgetContext | None = None,
        progress: ProgressCallback | None = None,
    ) -> DurableTurnOutcome:
        del provider_budget
        attached = TurnProgress(
            turn_id=self.turn_id,
            phase=TurnProgressPhase.ATTACHED,
            turn_status=TurnStatus.RUNNING,
        )
        await self.progress_hub.publish(attached)
        if progress is not None:
            await progress(attached)
        try:
            return await asyncio.shield(self._owner)
        except asyncio.CancelledError:
            self.wrapper_cancelled = True
            raise

    def query(
        self,
        _turn_id: str,
        _access: object,
        *,
        provider_budget: ProviderBudgetContext | None = None,
    ) -> DurableTurnOutcome:
        del provider_budget
        return _running(self.turn_id)

    async def cancel(self, *_args: object, **_kwargs: object) -> DurableTurnOutcome:
        self.cancel_calls += 1
        raise AssertionError("observer disconnect must not cancel the owner")


_TERMINAL = {
    TurnStatus.COMPLETED,
    TurnStatus.FAILED,
    TurnStatus.CANCELLED,
    TurnStatus.INTERRUPTED,
}


@pytest.mark.asyncio
async def test_completed_stream_maps_progress_and_emits_only_persisted_text() -> None:
    payload = _chat("completed")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    answer = "Đã kiểm chứng.\r\nKhông lộ dữ liệu SSE."
    outcome = _completed(turn_id, answer)
    service = _ScriptedTurnService(
        turn_id=turn_id,
        outcome=outcome,
        progress=(
            _progress(turn_id, TurnProgressPhase.ADMITTED, TurnStatus.PENDING),
            _progress(turn_id, TurnProgressPhase.CLAIMED, TurnStatus.RUNNING),
            _progress(
                turn_id,
                TurnProgressPhase.STEP_STARTED,
                TurnStatus.RUNNING,
                step_id="step_sse_search",
                capability="product.catalog.search",
                plan_revision=0,
            ),
            _progress(
                turn_id,
                TurnProgressPhase.STEP_FINISHED,
                TurnStatus.RUNNING,
                step_id="step_sse_search",
                capability="product.catalog.search",
                plan_revision=0,
                step_status=TaskStatus.SUCCESS,
            ),
        ),
        block=True,
        duplicate_terminal=True,
    )
    response = _response(service, payload)
    chunks: list[bytes] = []

    async def consume() -> None:
        async for chunk in _body(response):
            chunks.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(service.entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert service.persisted is False
    assert all(b"event: text_delta" not in chunk for chunk in chunks)
    service.release.set()
    await asyncio.wait_for(consumer, timeout=1)

    records = _parse_records(chunks)
    data_records = [record for record in records if "data" in record]
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert [record["event"] for record in data_records[:4]] == ["progress"] * 4
    assert [record["data"]["phase"] for record in data_records[:4]] == [
        "admitted",
        "claimed",
        "step_started",
        "step_finished",
    ]
    deltas = [
        record["data"]["delta"]
        for record in data_records
        if record["event"] == "text_delta"
    ]
    assert "".join(deltas) == answer
    assert all(
        record["data"]["turn_status"] == "completed"
        and record["data"]["post_grounding"] is True
        for record in data_records
        if record["event"] == "text_delta"
    )
    assert [record["event"] for record in data_records].count("terminal") == 1
    assert data_records[-1]["event"] == "terminal"
    assert data_records[-1]["data"]["server_settled"] is True
    _assert_order_and_correlation(data_records, turn_id=turn_id)
    wire = b"".join(chunks)
    assert payload.message.encode() not in wire
    assert b"\n" not in wire.replace(b"\r\n", b"")
    assert b"\r" not in wire.replace(b"\r\n", b"")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome_factory", "expected_status"),
    [
        (lambda turn_id: _failed(turn_id), "failed"),
        (lambda turn_id: _cancelled(turn_id), "cancelled"),
        (lambda turn_id: _interrupted(turn_id), "interrupted"),
    ],
)
async def test_each_durable_terminal_status_is_typed_once_and_final(
    outcome_factory: Callable[[str], DurableTurnOutcome],
    expected_status: str,
) -> None:
    payload = _chat(expected_status)
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    outcome = outcome_factory(turn_id)
    service = _ScriptedTurnService(
        turn_id=turn_id,
        outcome=outcome,
        progress=(
            _progress(turn_id, TurnProgressPhase.ADMITTED, TurnStatus.PENDING),
            _progress(turn_id, TurnProgressPhase.CLAIMED, TurnStatus.RUNNING),
        ),
        duplicate_terminal=True,
    )

    records = _data_records(await _collect(_response(service, payload)))

    terminals = [record for record in records if record["event"] == "terminal"]
    assert len(terminals) == 1
    assert records[-1] == terminals[0]
    assert terminals[0]["data"]["payload"]["status"] == expected_status
    assert terminals[0]["data"]["server_settled"] is True
    assert not any(record["event"] == "text_delta" for record in records)
    assert "private" not in json.dumps(records)
    _assert_order_and_correlation(records, turn_id=turn_id)


@pytest.mark.asyncio
async def test_timeout_heartbeats_do_not_consume_sequence_and_cancel_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v2_stream, "_HEARTBEAT_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(v2_stream, "_DURABLE_POLL_INTERVAL_SECONDS", 0.002)
    monkeypatch.setattr(v2_stream, "_STREAM_TIMEOUT_SECONDS", 0.025)
    payload = _chat("timeout")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    service = _ScriptedTurnService(
        turn_id=turn_id,
        outcome=_completed(turn_id, "late"),
        progress=(
            _progress(turn_id, TurnProgressPhase.ADMITTED, TurnStatus.PENDING),
            _progress(turn_id, TurnProgressPhase.CLAIMED, TurnStatus.RUNNING),
        ),
        block=True,
        cancel_outcome=_cancelled(turn_id),
    )

    chunks = await _collect(_response(service, payload))
    records = _parse_records(chunks)
    comments = [record for record in records if record.get("comment") == "heartbeat"]
    data_records = [record for record in records if "data" in record]

    assert comments
    assert all(set(comment) == {"comment"} for comment in comments)
    assert [record["data"]["sequence"] for record in data_records] == [1, 2, 3]
    assert data_records[-1]["event"] == "terminal"
    assert data_records[-1]["data"]["payload"]["status"] == "cancelled"
    assert data_records[-1]["data"]["server_settled"] is True
    assert service.cancel_calls == 1
    assert service.execution_cancelled is True


@pytest.mark.asyncio
async def test_cross_process_live_retry_polls_without_rerunning_or_cancelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v2_stream, "_DURABLE_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(v2_stream, "_STREAM_TIMEOUT_SECONDS", 1.0)
    payload = _chat("cross_process")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    running = _running(turn_id)
    completed = _completed(turn_id, "Kết quả đã lưu.", reused=True)
    service = _ScriptedTurnService(
        turn_id=turn_id,
        outcome=running,
        progress=(_progress(turn_id, TurnProgressPhase.ATTACHED, TurnStatus.RUNNING),),
        query_outcomes=(running, running, completed),
    )

    records = _data_records(await _collect(_response(service, payload)))

    assert service.execute_calls == 1
    assert service.query_calls == 3
    assert service.cancel_calls == 0
    assert records[0]["data"]["phase"] == "attached"
    assert records[-1]["event"] == "terminal"
    assert records[-1]["data"]["server_settled"] is True
    assert records[-1]["data"]["reused_result"] is True
    assert (
        "".join(
            record["data"]["delta"]
            for record in records
            if record["event"] == "text_delta"
        )
        == "Kết quả đã lưu."
    )


@pytest.mark.asyncio
async def test_passive_timeout_does_not_overwrite_a_live_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v2_stream, "_DURABLE_POLL_INTERVAL_SECONDS", 0.002)
    monkeypatch.setattr(v2_stream, "_STREAM_TIMEOUT_SECONDS", 0.015)
    payload = _chat("passive_timeout")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    running = _running(turn_id)
    service = _ScriptedTurnService(
        turn_id=turn_id,
        outcome=running,
        progress=(_progress(turn_id, TurnProgressPhase.ATTACHED, TurnStatus.RUNNING),),
        query_outcomes=(running,) * 20,
    )

    records = _data_records(await _collect(_response(service, payload)))

    assert service.execute_calls == 1
    assert service.query_calls > 0
    assert service.cancel_calls == 0
    assert service.persisted is False
    assert service.outcome.status is TurnStatus.RUNNING
    assert [record["event"] for record in records] == ["progress", "terminal"]
    assert records[0]["data"]["phase"] == "attached"
    terminal_payload = records[-1]["data"]["payload"]
    assert records[-1]["data"]["server_settled"] is False
    assert records[-1]["data"]["reused_result"] is False
    assert terminal_payload["status"] == "interrupted"
    assert terminal_payload["error"] == {
        "code": "stream.interrupted",
        "message": "Luồng kết quả bị gián đoạn. Vui lòng thử lại.",
        "retryable": True,
    }
    assert "raw private prompt" not in json.dumps(records)


@pytest.mark.asyncio
async def test_same_process_observer_disconnect_only_detaches_wrapper() -> None:
    payload = _chat("observer_disconnect")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    service = _LocalObserverService(turn_id, _completed(turn_id, "done"))

    await _collect(
        _response(
            service,
            payload,
            request=_RequestProbe(disconnect_on=2),
        )
    )
    await asyncio.sleep(0)

    assert service.cancel_calls == 0
    assert service.wrapper_cancelled is True
    assert service.owner_cancelled is False
    assert service._owner.done() is False
    service.release.set()
    await asyncio.wait_for(service.owner_finished.wait(), timeout=1)
    assert service.owner_cancelled is False


@dataclass(frozen=True, slots=True)
class _PostgresStore:
    sessions: sessionmaker[Session]
    ledger: SQLProviderBudgetLedger


@pytest.fixture(scope="module")
def postgres_store() -> Iterator[_PostgresStore]:
    with disposable_postgres_database("thanh_v2_p2_sse_") as database_url:
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
                    id="corpus_sse_adapter",
                    corpus_name="sse-adapter-fixture",
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
        ledger.create_account(account_id="sse-adapter-account")
        try:
            yield _PostgresStore(sessions=sessions, ledger=ledger)
        finally:
            engine.dispose()


class _PostgresBlockingHandler:
    def __init__(self, ledger: SQLProviderBudgetLedger) -> None:
        self.ledger = ledger
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

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
            request_fingerprint_sha256="d" * 64,
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
                input_tokens=8,
                cached_input_tokens=0,
                output_tokens=3,
                reasoning_tokens=0,
                total_tokens=11,
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
        return TurnComputation(
            result=TurnResult(
                outcome=DialogueOutcome.ANSWERED,
                answer="Một kết quả bền vững.",
            )
        )


class _ClaimBoundaryHub(TurnProgressHub):
    def __init__(self) -> None:
        super().__init__()
        self.claim_publication_started = asyncio.Event()
        self.release_claim_publication = asyncio.Event()

    async def publish(self, event: TurnProgress) -> None:
        if event.phase is TurnProgressPhase.CLAIMED:
            self.claim_publication_started.set()
            await self.release_claim_publication.wait()
        await super().publish(event)


class _CancelFailingService:
    def __init__(self, delegate: V2TurnService) -> None:
        self.delegate = delegate
        self.progress_hub = delegate.progress_hub
        self.cancel_calls = 0
        self.execute_calls = 0
        self.second_execute_started = asyncio.Event()

    async def execute(self, *args: Any, **kwargs: Any) -> DurableTurnOutcome:
        self.execute_calls += 1
        if self.execute_calls == 2:
            self.second_execute_started.set()
        return await self.delegate.execute(*args, **kwargs)

    def query(self, *args: Any, **kwargs: Any) -> DurableTurnOutcome:
        return self.delegate.query(*args, **kwargs)

    async def cancel(self, *_args: Any, **_kwargs: Any) -> DurableTurnOutcome:
        self.cancel_calls += 1
        raise RuntimeError("simulated durable cancellation outage")


@pytest.mark.asyncio
async def test_disconnect_at_claim_boundary_persists_cancel_and_retry_replays(
    postgres_store: _PostgresStore,
) -> None:
    context = _context("claim_boundary")
    payload = _chat("claim_boundary")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    _create_conversation(postgres_store.sessions, context, payload.conversation_id)
    postgres_store.ledger.create_scope(
        scope_id=turn_id,
        account_id="sse-adapter-account",
        purpose="chat",
    )
    budget = ProviderBudgetContext(
        ledger=postgres_store.ledger,
        scope_id=turn_id,
        purpose="chat",
    )
    handler = _PostgresBlockingHandler(postgres_store.ledger)
    hub = _ClaimBoundaryHub()
    service = V2TurnService(
        DurableReadTurnExecutor(
            postgres_store.sessions,
            handler,
            budget_ledger=postgres_store.ledger,
        ),
        progress_hub=hub,
        worker_id="worker_sse_claim_boundary",
    )
    resolved = _resolved(service, context=context, access=context.access)
    consumer = asyncio.create_task(
        _collect(
            _response(
                service,
                payload,
                resolved=resolved,
                provider_budget=budget,
            )
        )
    )

    await asyncio.wait_for(hub.claim_publication_started.wait(), timeout=2)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    hub.release_claim_publication.set()

    cancelled = service.query(turn_id, context.access, provider_budget=budget)
    retry_records = _data_records(
        await asyncio.wait_for(
            _collect(
                _response(
                    service,
                    payload,
                    request=_RequestProbe(suffix="claim-retry"),
                    resolved=resolved,
                    provider_budget=budget,
                )
            ),
            timeout=2,
        )
    )

    assert cancelled.status is TurnStatus.CANCELLED
    assert handler.calls == 0
    assert retry_records[-1]["event"] == "terminal"
    assert retry_records[-1]["data"]["payload"]["status"] == "cancelled"
    assert retry_records[-1]["data"]["reused_result"] is True


@pytest.mark.asyncio
async def test_cancel_write_failure_keeps_owner_alive_and_retry_attaches_once(
    postgres_store: _PostgresStore,
) -> None:
    context = _context("cancel_failure")
    payload = _chat("cancel_failure")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    _create_conversation(postgres_store.sessions, context, payload.conversation_id)
    postgres_store.ledger.create_scope(
        scope_id=turn_id,
        account_id="sse-adapter-account",
        purpose="chat",
    )
    budget = ProviderBudgetContext(
        ledger=postgres_store.ledger,
        scope_id=turn_id,
        purpose="chat",
    )
    handler = _PostgresBlockingHandler(postgres_store.ledger)
    delegate = V2TurnService(
        DurableReadTurnExecutor(
            postgres_store.sessions,
            handler,
            budget_ledger=postgres_store.ledger,
        ),
        worker_id="worker_sse_cancel_failure",
    )
    service = _CancelFailingService(delegate)
    resolved = _resolved(service, context=context, access=context.access)

    first = _response(
        service,
        payload,
        request=_RequestProbe(disconnect_on=2, suffix="owner"),
        resolved=resolved,
        provider_budget=budget,
    )
    await _collect(first)
    await asyncio.wait_for(handler.entered.wait(), timeout=2)

    assert service.cancel_calls == 1
    assert handler.calls == 1
    assert delegate.query(turn_id, context.access).status is TurnStatus.RUNNING

    retry = _response(
        service,
        payload,
        request=_RequestProbe(suffix="retry"),
        resolved=resolved,
        provider_budget=budget,
    )
    retry_task = asyncio.create_task(_collect(retry))
    await asyncio.wait_for(service.second_execute_started.wait(), timeout=2)
    handler.release.set()
    retry_records = _data_records(await asyncio.wait_for(retry_task, timeout=5))

    assert handler.calls == 1
    assert service.execute_calls == 2
    assert service.cancel_calls == 1
    assert postgres_store.ledger.scope_usage_summary(turn_id).provider_attempts == 1
    assert retry_records[-1]["event"] == "terminal"
    assert retry_records[-1]["data"]["payload"]["status"] == "completed"
    assert retry_records[-1]["data"]["reused_result"] is True


def test_crlf_frames_survive_arbitrary_byte_chunk_boundaries() -> None:
    payload = _chat("chunking")
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    event = v2_stream._terminal_frames(
        _completed(turn_id, "Tiếng Việt 📚"),
        start_sequence=0,
        request_id="request_sse_chunk",
        trace_id="trace_sse_chunk",
        claimed_by_this_stream=True,
        server_settled=True,
    )
    assert event is not None
    _, frames = event
    wire = b"".join(frames)
    chunks = [wire[index : index + size] for index, size in _chunk_ranges(len(wire))]

    restored = _parse_records(chunks)

    assert [record["event"] for record in restored] == ["text_delta", "terminal"]
    assert restored[0]["data"]["delta"] == "Tiếng Việt 📚"
    assert restored[-1]["data"]["server_settled"] is True


async def _body(response: StreamingResponse) -> AsyncIterator[bytes]:
    async for chunk in response.body_iterator:
        yield chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")


async def _collect(response: StreamingResponse) -> list[bytes]:
    return [chunk async for chunk in _body(response)]


def _response(
    service: object,
    payload: ChatRequest,
    *,
    request: _RequestProbe | None = None,
    resolved: ResolvedV2Runtime | None = None,
    provider_budget: ProviderBudgetContext | None = None,
) -> StreamingResponse:
    probe = request or _RequestProbe()
    runtime = resolved or _resolved(service)
    return build_v2_streaming_response(
        request=cast(Request, probe),
        payload=payload,
        resolved=runtime,
        provider_budget=provider_budget,
    )


def _resolved(
    service: object,
    *,
    context: object | None = None,
    access: object | None = None,
) -> ResolvedV2Runtime:
    return cast(
        ResolvedV2Runtime,
        SimpleNamespace(
            services=SimpleNamespace(turn_service=service),
            planning_context=context or object(),
            access=access or object(),
        ),
    )


def _chat(key: str) -> ChatRequest:
    return ChatRequest(
        conversation_id=f"conv_sse_{key}",
        client_turn_id=f"client-sse-{key}",
        message="raw private prompt must never be streamed",
    )


def _progress(
    turn_id: str,
    phase: TurnProgressPhase,
    status: TurnStatus,
    *,
    step_id: str | None = None,
    capability: str | None = None,
    plan_revision: int | None = None,
    step_status: TaskStatus | None = None,
) -> TurnProgress:
    return TurnProgress(
        turn_id=turn_id,
        phase=phase,
        turn_status=status,
        step_id=step_id,
        capability=capability,
        plan_revision=plan_revision,
        step_status=step_status,
    )


def _running(turn_id: str) -> DurableTurnOutcome:
    return DurableTurnOutcome(turn_id=turn_id, status=TurnStatus.RUNNING, reused=True)


def _completed(
    turn_id: str,
    answer: str,
    *,
    reused: bool = False,
) -> DurableTurnOutcome:
    result = TurnResult(outcome=DialogueOutcome.ANSWERED, answer=answer)
    return DurableTurnOutcome(
        turn_id=turn_id,
        status=TurnStatus.COMPLETED,
        outcome=result.outcome,
        result=result,
        reused=reused,
    )


def _failed(turn_id: str) -> DurableTurnOutcome:
    return DurableTurnOutcome(
        turn_id=turn_id,
        status=TurnStatus.FAILED,
        error=SafeExecutionError(
            code="provider.timeout",
            message="Safe public failure.",
            retryable=True,
        ),
    )


def _cancelled(turn_id: str) -> DurableTurnOutcome:
    return DurableTurnOutcome(turn_id=turn_id, status=TurnStatus.CANCELLED)


def _interrupted(turn_id: str) -> DurableTurnOutcome:
    return DurableTurnOutcome(
        turn_id=turn_id,
        status=TurnStatus.INTERRUPTED,
        error=SafeExecutionError(
            code="turn.interrupted",
            message="Safe public interruption.",
            retryable=True,
        ),
    )


def _parse_records(chunks: list[bytes] | tuple[bytes, ...]) -> list[dict[str, Any]]:
    buffer = b""
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        buffer += chunk
        while b"\r\n\r\n" in buffer:
            raw, buffer = buffer.split(b"\r\n\r\n", 1)
            lines = raw.decode("utf-8").split("\r\n")
            if lines[0].startswith(":"):
                records.append({"comment": lines[0][1:].strip()})
                continue
            fields: dict[str, Any] = {}
            for line in lines:
                name, value = line.split(":", 1)
                fields[name] = value.lstrip()
            fields["data"] = json.loads(fields["data"])
            records.append(fields)
    assert buffer == b""
    return records


def _data_records(chunks: list[bytes]) -> list[dict[str, Any]]:
    return [record for record in _parse_records(chunks) if "data" in record]


def _assert_order_and_correlation(
    records: list[dict[str, Any]],
    *,
    turn_id: str,
) -> None:
    sequences = [record["data"]["sequence"] for record in records]
    assert sequences == sorted(set(sequences))
    assert [int(record["id"]) for record in records] == sequences
    assert {record["data"]["request_id"] for record in records} == {"request_sse_001"}
    assert {record["data"]["trace_id"] for record in records} == {"trace_sse_001"}
    assert {record["data"]["turn_id"] for record in records} == {turn_id}


def _chunk_ranges(length: int) -> Iterator[tuple[int, int]]:
    start = 0
    sizes = (1, 2, 5, 3, 8, 13)
    index = 0
    while start < length:
        size = sizes[index % len(sizes)]
        yield start, size
        start += size
        index += 1


def _context(key: str) -> PlanningContext:
    authorization = AuthorizationContext(
        tenant_id=f"tenant_sse_{key}",
        principal_id=f"principal_sse_{key}",
        scopes=frozenset({"ecommerce.read"}),
    )
    return PlanningContext(
        access=bind_request_authorization(authorization, ConversationMode.SHOPPER),
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_sse_adapter",
            corpus_version_id="corpus_sse_adapter",
            index_manifest_id="index_sse_adapter",
        ),
    )


def _create_conversation(
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
