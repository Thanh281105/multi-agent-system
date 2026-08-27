"""Authenticated JSON and SSE entry points for the multi-agent orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.contracts import AuthorizationContext, TaskStatus
from app.gateway.dependencies import (
    PrincipalContext,
    authorize_request,
    get_runtime,
)
from app.gateway.errors import GatewayAPIError
from app.gateway.runtime import GatewayRuntime
from app.gateway.schemas import (
    GatewayChatRequest,
    GatewayChatResponse,
    GatewayErrorDetail,
    GatewayErrorResponse,
    GatewayStatusEvent,
    build_chat_response,
)
from app.gateway.sse import encode_sse, heartbeat
from app.gateway.turns import SessionTurnBusyError
from app.orchestrator import OrchestrationResult
from app.orchestrator.progress import OrchestrationProgress, ProgressCallback
from app.shared import (
    SessionExpiredError,
    SessionNotFoundError,
    SessionOwnershipError,
    SharedStateUnavailableError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["chat-v1"])


@router.post("/chat", response_model=GatewayChatResponse)
async def multi_agent_chat(
    payload: GatewayChatRequest,
    request: Request,
    principal: Annotated[PrincipalContext, Depends(authorize_request)],
) -> GatewayChatResponse:
    """Run one authenticated turn through the multi-agent orchestrator."""

    runtime = get_runtime(request)
    _validate_existing_session(runtime, principal.principal_id, payload.session_id)
    result = await _execute_turn(
        runtime=runtime,
        payload=payload,
        principal_id=principal.principal_id,
        authorization=principal.authorization,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )
    return build_chat_response(result)


@router.post("/chat/stream", response_class=StreamingResponse)
async def multi_agent_chat_stream(
    payload: GatewayChatRequest,
    request: Request,
    principal: Annotated[PrincipalContext, Depends(authorize_request)],
) -> StreamingResponse:
    """Stream real orchestration transitions and exactly one terminal event."""

    runtime = get_runtime(request)
    _validate_existing_session(runtime, principal.principal_id, payload.session_id)
    return StreamingResponse(
        _stream_turn(
            runtime=runtime,
            payload=payload,
            principal_id=principal.principal_id,
            authorization=principal.authorization,
            request_id=request.state.request_id,
            trace_id=request.state.trace_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _execute_turn(
    *,
    runtime: GatewayRuntime,
    payload: GatewayChatRequest,
    principal_id: str,
    authorization: AuthorizationContext,
    request_id: str,
    trace_id: str,
    progress: ProgressCallback | None = None,
) -> OrchestrationResult:
    lock_key = payload.session_id or request_id
    try:
        async with runtime.turns.turn(lock_key):
            async with asyncio.timeout(runtime.config.orchestration_timeout_seconds):
                result = await runtime.orchestrator.run(
                    message=payload.message,
                    principal_id=principal_id,
                    authorization=authorization,
                    session_id=payload.session_id,
                    request_id=request_id,
                    trace_id=trace_id,
                    progress=progress,
                )
    except TimeoutError:
        raise GatewayAPIError(
            status_code=504,
            code="gateway.orchestration_timeout",
            message="Tác vụ vượt quá thời gian xử lý cho phép.",
            retryable=True,
        ) from None
    except SessionTurnBusyError:
        raise GatewayAPIError(
            status_code=409,
            code="gateway.session_busy",
            message="Session đang xử lý một yêu cầu khác.",
            retryable=True,
        ) from None
    except (SessionNotFoundError, SessionExpiredError, SessionOwnershipError):
        raise _session_not_found() from None
    except SharedStateUnavailableError:
        raise GatewayAPIError(
            status_code=503,
            code="gateway.shared_state_unavailable",
            message="Dịch vụ session tạm thời không khả dụng.",
            retryable=True,
        ) from None

    if result.status == TaskStatus.FAILED:
        raise GatewayAPIError(
            status_code=503,
            code="gateway.all_agents_failed",
            message="Không thể hoàn tất yêu cầu từ các nguồn dữ liệu hiện có.",
            retryable=True,
        )
    return result


async def _stream_turn(
    *,
    runtime: GatewayRuntime,
    payload: GatewayChatRequest,
    principal_id: str,
    authorization: AuthorizationContext,
    request_id: str,
    trace_id: str,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[OrchestrationProgress | None] = asyncio.Queue(maxsize=100)
    sequence = 1

    async def on_progress(event: OrchestrationProgress) -> None:
        await queue.put(event)

    async def execute() -> OrchestrationResult:
        try:
            return await _execute_turn(
                runtime=runtime,
                payload=payload,
                principal_id=principal_id,
                authorization=authorization,
                request_id=request_id,
                trace_id=trace_id,
                progress=on_progress,
            )
        finally:
            await queue.put(None)

    accepted = GatewayStatusEvent(
        sequence=sequence,
        phase="request.accepted",
        message="Gateway đã xác thực và tiếp nhận yêu cầu.",
        request_id=request_id,
        trace_id=trace_id,
        status=TaskStatus.RUNNING,
    )
    yield encode_sse(
        "status",
        accepted,
        event_id=f"{request_id}:{sequence}",
        retry_ms=3_000,
    )
    task = asyncio.create_task(execute())
    try:
        while True:
            try:
                progress = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield heartbeat()
                continue
            if progress is None:
                break
            sequence += 1
            status_event = GatewayStatusEvent(
                sequence=sequence,
                phase=progress.phase,
                message=progress.message,
                request_id=request_id,
                trace_id=trace_id,
                step_id=progress.step_id,
                agent_id=progress.agent_id,
                status=progress.status,
            )
            yield encode_sse(
                "status",
                status_event,
                event_id=f"{request_id}:{sequence}",
            )

        try:
            result = await task
            sequence += 1
            yield encode_sse(
                "completed",
                build_chat_response(result),
                event_id=f"{request_id}:{sequence}",
            )
        except GatewayAPIError as exc:
            sequence += 1
            yield encode_sse(
                "error",
                _stream_error(exc, request_id=request_id, trace_id=trace_id),
                event_id=f"{request_id}:{sequence}",
            )
        except Exception as exc:
            logger.error(
                "SSE_UNHANDLED request_id=%s trace_id=%s error_type=%s",
                request_id,
                trace_id,
                type(exc).__name__,
            )
            sequence += 1
            yield encode_sse(
                "error",
                _stream_error(
                    GatewayAPIError(
                        status_code=500,
                        code="gateway.internal_error",
                        message="Không thể xử lý yêu cầu lúc này.",
                        retryable=True,
                    ),
                    request_id=request_id,
                    trace_id=trace_id,
                ),
                event_id=f"{request_id}:{sequence}",
            )
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _validate_existing_session(
    runtime: GatewayRuntime,
    principal_id: str,
    session_id: str | None,
) -> None:
    if session_id is None:
        return
    try:
        runtime.orchestrator.sessions.get(
            owner_id=principal_id,
            session_id=session_id,
        )
    except (SessionNotFoundError, SessionExpiredError, SessionOwnershipError):
        raise _session_not_found() from None
    except SharedStateUnavailableError:
        raise GatewayAPIError(
            status_code=503,
            code="gateway.shared_state_unavailable",
            message="Dịch vụ session tạm thời không khả dụng.",
            retryable=True,
        ) from None


def _session_not_found() -> GatewayAPIError:
    return GatewayAPIError(
        status_code=404,
        code="gateway.session_not_found",
        message="Session không tồn tại hoặc không thuộc principal hiện tại.",
    )


def _stream_error(
    error: GatewayAPIError,
    *,
    request_id: str,
    trace_id: str,
) -> GatewayErrorResponse:
    return GatewayErrorResponse(
        error=GatewayErrorDetail(
            code=error.code,
            message=error.message,
            request_id=request_id,
            trace_id=trace_id,
            retryable=error.retryable,
        )
    )
