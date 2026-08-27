"""Backward-compatible Phase 1 endpoint for baseline evaluation only."""

from __future__ import annotations

import logging
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.agent.runner import AgentRunError, run_agent
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    response: Response,
) -> ChatResponse:
    """Run a Vietnamese user message through the single OpenAI agent."""

    request_id = request.state.request_id
    session_id = payload.session_id or f"sess_{request_id.removeprefix('req_')}"
    started_at = perf_counter()
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "LEGACY_REQUEST request_id=%s message_length=%d",
        request_id,
        len(payload.message),
    )

    try:
        result = await run_agent(
            message=payload.message,
            session_id=session_id,
            request_id=request_id,
        )
    except AgentRunError:
        logger.error(
            "LEGACY_CHAT_ERROR request_id=%s latency_ms=%d",
            request_id,
            int((perf_counter() - started_at) * 1000),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Không thể xử lý yêu cầu lúc này.",
        ) from None
    except Exception:
        logger.error(
            "LEGACY_CHAT_UNEXPECTED request_id=%s latency_ms=%d",
            request_id,
            int((perf_counter() - started_at) * 1000),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Không thể xử lý yêu cầu lúc này.",
        ) from None

    logger.info(
        "LEGACY_CHAT_COMPLETE request_id=%s latency_ms=%d",
        request_id,
        int((perf_counter() - started_at) * 1000),
    )
    return ChatResponse(
        answer=result.answer,
        tool_calls=result.tool_calls,
        session_id=session_id,
    )
