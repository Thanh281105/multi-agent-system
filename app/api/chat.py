"""FastAPI endpoints for health and agent chat."""

from __future__ import annotations

import logging
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.agent.runner import AgentRunError, run_agent
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness endpoint that does not require an LLM or database call."""

    return {"status": "ok"}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    response: Response,
) -> ChatResponse:
    """Run a Vietnamese user message through the single OpenAI agent."""

    request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex[:12]}"
    session_id = payload.session_id or f"sess_{uuid4().hex}"
    started_at = perf_counter()
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "REQUEST request_id=%s session_id=%s message=%r",
        request_id,
        session_id,
        payload.message,
    )

    try:
        result = await run_agent(
            message=payload.message,
            session_id=session_id,
            request_id=request_id,
        )
    except AgentRunError:
        logger.exception(
            "CHAT ERROR request_id=%s session_id=%s latency_ms=%d",
            request_id,
            session_id,
            int((perf_counter() - started_at) * 1000),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Không thể xử lý yêu cầu lúc này.",
        ) from None
    except Exception:
        logger.exception(
            "CHAT UNEXPECTED ERROR request_id=%s session_id=%s latency_ms=%d",
            request_id,
            session_id,
            int((perf_counter() - started_at) * 1000),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Không thể xử lý yêu cầu lúc này.",
        ) from None

    logger.info(
        "CHAT COMPLETE request_id=%s session_id=%s latency_ms=%d",
        request_id,
        session_id,
        int((perf_counter() - started_at) * 1000),
    )
    return ChatResponse(
        answer=result.answer,
        tool_calls=result.tool_calls,
        session_id=session_id,
    )
