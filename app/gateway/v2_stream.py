"""Durable, recovery-safe Server-Sent Events for public v2 chat turns."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.shared.budget import ProviderBudgetContext
from app.v2.contracts import (
    ChatRequest,
    SafeExecutionError,
    TurnCancelledTerminal,
    TurnCompletedTerminal,
    TurnFailedTerminal,
    TurnInterruptedTerminal,
    TurnSSEEvent,
    TurnSSEProgressEvent,
    TurnSSEProgressPhase,
    TurnSSETerminalEvent,
    TurnSSETextDeltaEvent,
    TurnStatus,
    TurnTerminalPayload,
    UsageSummary,
)
from app.v2.execution import DurableTurnOutcome
from app.v2.progress import TurnProgress, TurnProgressPhase
from app.v2.runtime import ResolvedV2Runtime
from app.v2.turn_service import canonical_turn_id

_HEARTBEAT_INTERVAL_SECONDS = 10.0
_DURABLE_POLL_INTERVAL_SECONDS = 0.25
_STREAM_TIMEOUT_SECONDS = 75.0
_TEXT_DELTA_SIZE = 512

_TERMINAL_STATUSES = frozenset(
    {
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
        TurnStatus.INTERRUPTED,
    }
)


def build_v2_streaming_response(
    *,
    request: Request,
    payload: ChatRequest,
    resolved: ResolvedV2Runtime,
    provider_budget: ProviderBudgetContext | None = None,
) -> StreamingResponse:
    """Build the public stream without bypassing canonical turn admission."""

    return StreamingResponse(
        _stream_v2_turn(
            request=request,
            payload=payload,
            resolved=resolved,
            provider_budget=provider_budget,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_v2_turn(
    *,
    request: Request,
    payload: ChatRequest,
    resolved: ResolvedV2Runtime,
    provider_budget: ProviderBudgetContext | None,
) -> AsyncIterator[bytes]:
    service = resolved.services.turn_service
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    request_id = request.state.request_id
    trace_id = request.state.trace_id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STREAM_TIMEOUT_SECONDS
    next_heartbeat = loop.time() + _HEARTBEAT_INTERVAL_SECONDS
    next_poll = loop.time()
    sequence = 0
    claimed_by_this_stream = False
    cancellation_attempted = False
    durable_terminal_seen = False
    terminal_emitted = False
    execution_observed = False
    execution_failed = False
    outcome: DurableTurnOutcome | None = None
    terminal_hint = False
    pending: deque[TurnProgress] = deque()

    async def observe_this_invocation(event: TurnProgress) -> None:
        nonlocal claimed_by_this_stream
        if event.turn_id == turn_id and event.phase is TurnProgressPhase.CLAIMED:
            claimed_by_this_stream = True

    async with service.progress_hub.subscribe(turn_id) as progress_queue:
        execution_task = asyncio.create_task(
            service.execute(
                payload,
                resolved.planning_context,
                provider_budget=provider_budget,
                progress=observe_this_invocation,
            )
        )

        async def cancel_owner() -> DurableTurnOutcome | None:
            nonlocal cancellation_attempted, durable_terminal_seen
            if not claimed_by_this_stream or cancellation_attempted:
                return None
            cancellation_attempted = True
            cancelled = await _cancel_durably(
                resolved,
                turn_id,
                provider_budget=provider_budget,
            )
            if cancelled is not None and cancelled.status in _TERMINAL_STATUSES:
                durable_terminal_seen = True
                if not execution_task.done():
                    execution_task.cancel()
            return cancelled

        try:
            while True:
                while True:
                    try:
                        pending.append(progress_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                while pending:
                    progress = pending.popleft()
                    if progress.turn_id != turn_id:
                        continue
                    if progress.phase is TurnProgressPhase.TERMINAL:
                        terminal_hint = True
                        continue
                    sequence += 1
                    event = _progress_event(
                        progress,
                        sequence=sequence,
                        request_id=request_id,
                        trace_id=trace_id,
                    )
                    yield _encode_event(event)

                if not execution_observed and execution_task.done():
                    execution_observed = True
                    try:
                        outcome = execution_task.result()
                    except asyncio.CancelledError:
                        execution_failed = True
                    except Exception:
                        execution_failed = True

                if outcome is not None and outcome.status in _TERMINAL_STATUSES:
                    durable_terminal_seen = True
                    terminal = _terminal_frames_or_safe(
                        outcome,
                        start_sequence=sequence,
                        request_id=request_id,
                        trace_id=trace_id,
                        claimed_by_this_stream=claimed_by_this_stream,
                    )
                    sequence, frames = terminal
                    for frame in frames:
                        yield frame
                    terminal_emitted = True
                    return

                if execution_failed:
                    reconciled = await cancel_owner()
                    if (
                        reconciled is None
                        or reconciled.status not in _TERMINAL_STATUSES
                    ):
                        queried = await _query_durably(
                            resolved,
                            turn_id,
                            provider_budget=provider_budget,
                        )
                        if queried is not None:
                            reconciled = queried
                    if (
                        reconciled is not None
                        and reconciled.status in _TERMINAL_STATUSES
                    ):
                        durable_terminal_seen = True
                        terminal = _terminal_frames_or_safe(
                            reconciled,
                            start_sequence=sequence,
                            request_id=request_id,
                            trace_id=trace_id,
                            claimed_by_this_stream=claimed_by_this_stream,
                        )
                        sequence, frames = terminal
                        for frame in frames:
                            yield frame
                        terminal_emitted = True
                        return
                    outcome = reconciled
                    execution_failed = False

                if await _request_is_disconnected(request):
                    await cancel_owner()
                    return

                now = loop.time()
                if now >= deadline:
                    timed_out = await cancel_owner()
                    if timed_out is None or timed_out.status not in _TERMINAL_STATUSES:
                        queried = await _query_durably(
                            resolved,
                            turn_id,
                            provider_budget=provider_budget,
                        )
                        if queried is not None:
                            timed_out = queried
                    if timed_out is not None and timed_out.status in _TERMINAL_STATUSES:
                        durable_terminal_seen = True
                        terminal_outcome = timed_out
                    else:
                        terminal_outcome = _safe_interrupted_outcome(
                            turn_id,
                            timed_out or outcome,
                        )
                    terminal = _terminal_frames_or_safe(
                        terminal_outcome,
                        start_sequence=sequence,
                        request_id=request_id,
                        trace_id=trace_id,
                        claimed_by_this_stream=claimed_by_this_stream,
                    )
                    sequence, frames = terminal
                    for frame in frames:
                        yield frame
                    terminal_emitted = True
                    return

                should_poll = execution_observed or terminal_hint
                if should_poll and now >= next_poll:
                    queried = await _query_durably(
                        resolved,
                        turn_id,
                        provider_budget=provider_budget,
                    )
                    next_poll = loop.time() + _DURABLE_POLL_INTERVAL_SECONDS
                    if queried is not None:
                        outcome = queried
                        if queried.status in _TERMINAL_STATUSES:
                            durable_terminal_seen = True
                            continue

                now = loop.time()
                if now >= next_heartbeat:
                    yield _heartbeat()
                    next_heartbeat = now + _HEARTBEAT_INTERVAL_SECONDS
                    continue

                wait_seconds = min(
                    max(0.0, deadline - now),
                    max(0.0, next_heartbeat - now),
                    _DURABLE_POLL_INTERVAL_SECONDS,
                )
                if should_poll:
                    wait_seconds = min(wait_seconds, max(0.0, next_poll - now))

                queue_task = asyncio.create_task(progress_queue.get())
                waiters: set[asyncio.Task[object]] = {queue_task}
                if not execution_observed:
                    waiters.add(execution_task)
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=wait_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_task in done:
                    pending.append(queue_task.result())
                else:
                    queue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_task
        finally:
            if (
                claimed_by_this_stream
                and not durable_terminal_seen
                and not terminal_emitted
            ):
                await cancel_owner()
            should_await_execution = True
            if not execution_task.done():
                if claimed_by_this_stream and not durable_terminal_seen:
                    execution_task.add_done_callback(_consume_task_result)
                    should_await_execution = False
                else:
                    execution_task.cancel()
            if should_await_execution:
                with suppress(asyncio.CancelledError, Exception):
                    await execution_task


async def _query_durably(
    resolved: ResolvedV2Runtime,
    turn_id: str,
    *,
    provider_budget: ProviderBudgetContext | None,
) -> DurableTurnOutcome | None:
    try:
        return await asyncio.to_thread(
            resolved.services.turn_service.query,
            turn_id,
            resolved.access,
            provider_budget=provider_budget,
        )
    except Exception:
        return None


async def _cancel_durably(
    resolved: ResolvedV2Runtime,
    turn_id: str,
    *,
    provider_budget: ProviderBudgetContext | None,
) -> DurableTurnOutcome | None:
    try:
        return await resolved.services.turn_service.cancel(
            turn_id,
            resolved.access,
            provider_budget=provider_budget,
        )
    except Exception:
        return None


async def _request_is_disconnected(request: Request) -> bool:
    try:
        return await request.is_disconnected()
    except Exception:
        return False


def _progress_event(
    progress: TurnProgress,
    *,
    sequence: int,
    request_id: str,
    trace_id: str,
) -> TurnSSEProgressEvent:
    return TurnSSEProgressEvent(
        sequence=sequence,
        request_id=request_id,
        trace_id=trace_id,
        turn_id=progress.turn_id,
        phase=TurnSSEProgressPhase(progress.phase.value),
        turn_status=progress.turn_status,
        step_id=progress.step_id,
        capability=progress.capability,
        plan_revision=progress.plan_revision,
        step_status=progress.step_status,
        reused=progress.reused,
    )


def _terminal_frames(
    outcome: DurableTurnOutcome,
    *,
    start_sequence: int,
    request_id: str,
    trace_id: str,
    claimed_by_this_stream: bool,
) -> tuple[int, tuple[bytes, ...]] | None:
    sequence = start_sequence
    frames: list[bytes] = []
    payload: TurnTerminalPayload

    if outcome.status is TurnStatus.COMPLETED:
        if outcome.result is None:
            return None
        for delta in _answer_chunks(outcome.result.answer):
            sequence += 1
            frames.append(
                _encode_event(
                    TurnSSETextDeltaEvent(
                        sequence=sequence,
                        delta=delta,
                        request_id=request_id,
                        trace_id=trace_id,
                        turn_id=outcome.turn_id,
                    )
                )
            )
        payload = TurnCompletedTerminal(
            status=TurnStatus.COMPLETED,
            result=outcome.result,
            usage=outcome.usage,
        )
    elif outcome.status is TurnStatus.FAILED:
        if outcome.error is None:
            return None
        payload = TurnFailedTerminal(
            status=TurnStatus.FAILED,
            error=outcome.error,
            usage=outcome.usage,
        )
    elif outcome.status is TurnStatus.CANCELLED:
        payload = TurnCancelledTerminal(
            status=TurnStatus.CANCELLED,
            usage=outcome.usage,
        )
    elif outcome.status is TurnStatus.INTERRUPTED:
        if outcome.error is None:
            return None
        payload = TurnInterruptedTerminal(
            status=TurnStatus.INTERRUPTED,
            error=outcome.error,
            usage=outcome.usage,
        )
    else:
        return None

    sequence += 1
    frames.append(
        _encode_event(
            TurnSSETerminalEvent(
                sequence=sequence,
                payload=payload,
                reused_result=outcome.reused and not claimed_by_this_stream,
                request_id=request_id,
                trace_id=trace_id,
                turn_id=outcome.turn_id,
            )
        )
    )
    return sequence, tuple(frames)


def _terminal_frames_or_safe(
    outcome: DurableTurnOutcome,
    *,
    start_sequence: int,
    request_id: str,
    trace_id: str,
    claimed_by_this_stream: bool,
) -> tuple[int, tuple[bytes, ...]]:
    terminal = _terminal_frames(
        outcome,
        start_sequence=start_sequence,
        request_id=request_id,
        trace_id=trace_id,
        claimed_by_this_stream=claimed_by_this_stream,
    )
    if terminal is not None:
        return terminal
    safe_terminal = _terminal_frames(
        _safe_interrupted_outcome(outcome.turn_id, outcome),
        start_sequence=start_sequence,
        request_id=request_id,
        trace_id=trace_id,
        claimed_by_this_stream=claimed_by_this_stream,
    )
    if safe_terminal is None:
        raise AssertionError("safe interrupted terminal must serialize")
    return safe_terminal


def _safe_interrupted_outcome(
    turn_id: str,
    previous: DurableTurnOutcome | None,
) -> DurableTurnOutcome:
    return DurableTurnOutcome(
        turn_id=turn_id,
        status=TurnStatus.INTERRUPTED,
        error=SafeExecutionError(
            code="stream.interrupted",
            message="Luồng kết quả bị gián đoạn. Vui lòng thử lại.",
            retryable=True,
        ),
        usage=previous.usage if previous is not None else UsageSummary(),
    )


def _answer_chunks(answer: str) -> Iterator[str]:
    for start in range(0, len(answer), _TEXT_DELTA_SIZE):
        yield answer[start : start + _TEXT_DELTA_SIZE]


def _encode_event(event: TurnSSEEvent) -> bytes:
    payload = event.model_dump_json()
    return (
        f"id: {event.sequence}\r\nevent: {event.event.value}\r\ndata: {payload}\r\n\r\n"
    ).encode("utf-8")


def _heartbeat() -> bytes:
    return b": heartbeat\r\n\r\n"


def _consume_task_result(task: asyncio.Task[DurableTurnOutcome]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        return


__all__ = ["build_v2_streaming_response"]
