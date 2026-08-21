"""Minimal Google ADK Runner wrapper used by the FastAPI layer."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent.agent import root_agent
from app.core.config import settings
from app.schemas.chat import ToolCallInfo

logger = logging.getLogger(__name__)


class AgentRunError(RuntimeError):
    """Raised when ADK cannot complete an agent invocation."""


@dataclass(frozen=True)
class AgentRunResult:
    """API-neutral result produced after consuming the ADK event stream."""

    answer: str
    tool_calls: list[ToolCallInfo]


runner = InMemoryRunner(agent=root_agent, app_name=settings.adk_app_name)
runner_lock = asyncio.Lock()


async def run_agent(
    *,
    message: str,
    session_id: str,
    request_id: str,
    user_id: str = "api-user",
) -> AgentRunResult:
    """Run one user turn and collect only safe response/tool-call metadata."""

    started_at = perf_counter()
    logger.info(
        "AGENT START request_id=%s session_id=%s model=%s",
        request_id,
        session_id,
        settings.adk_model,
    )

    async with runner_lock:
        try:
            await _ensure_session(user_id=user_id, session_id=session_id)
            content = types.Content(
                role="user",
                parts=[types.Part.from_text(text=message)],
            )
            tool_calls: list[dict[str, Any]] = []
            answer = ""

            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=content,
            ):
                for function_call in event.get_function_calls():
                    call_info = {
                        "name": function_call.name or "unknown_tool",
                        "arguments": dict(function_call.args or {}),
                        "result_summary": None,
                    }
                    tool_calls.append(call_info)
                    logger.info(
                        "ADK TOOL CALL request_id=%s session_id=%s "
                        "tool=%s arguments=%s",
                        request_id,
                        session_id,
                        call_info["name"],
                        call_info["arguments"],
                    )

                for function_response in event.get_function_responses():
                    summary = _summarize_tool_result(function_response.response)
                    _attach_result_summary(
                        tool_calls,
                        function_response.name or "unknown_tool",
                        summary,
                    )
                    logger.info(
                        "TOOL RESULT request_id=%s session_id=%s tool=%s summary=%s",
                        request_id,
                        session_id,
                        function_response.name or "unknown_tool",
                        summary,
                    )

                if event.is_final_response():
                    event_text = _extract_text(event.content)
                    if event_text:
                        answer = event_text

            if not answer:
                raise AgentRunError("ADK returned no final text response")

            result = AgentRunResult(
                answer=answer,
                tool_calls=[ToolCallInfo.model_validate(call) for call in tool_calls],
            )
            logger.info(
                "AGENT COMPLETE request_id=%s session_id=%s "
                "tool_calls=%d latency_ms=%d",
                request_id,
                session_id,
                len(result.tool_calls),
                int((perf_counter() - started_at) * 1000),
            )
            return result
        except AgentRunError:
            logger.exception(
                "AGENT ERROR request_id=%s session_id=%s latency_ms=%d",
                request_id,
                session_id,
                int((perf_counter() - started_at) * 1000),
            )
            raise
        except Exception as exc:
            logger.exception(
                "AGENT ERROR request_id=%s session_id=%s latency_ms=%d",
                request_id,
                session_id,
                int((perf_counter() - started_at) * 1000),
            )
            raise AgentRunError("ADK runtime failed") from exc


async def _ensure_session(*, user_id: str, session_id: str) -> None:
    existing = await runner.session_service.get_session(
        app_name=settings.adk_app_name,
        user_id=user_id,
        session_id=session_id,
    )
    if existing is None:
        await runner.session_service.create_session(
            app_name=settings.adk_app_name,
            user_id=user_id,
            session_id=session_id,
        )


def _extract_text(content: types.Content | None) -> str:
    if content is None:
        return ""
    text_parts = [part.text for part in content.parts or [] if part.text]
    return "".join(text_parts).strip()


def _summarize_tool_result(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None

    summary: dict[str, Any] = {}
    for key in ("found", "count"):
        if key in response and isinstance(response[key], (bool, int, float, str)):
            summary[key] = response[key]
    for key in ("products", "reviews", "missing_product_ids"):
        value = response.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    return summary or None


def _attach_result_summary(
    tool_calls: list[dict[str, Any]],
    tool_name: str,
    summary: dict[str, Any] | None,
) -> None:
    for call in reversed(tool_calls):
        if call["name"] == tool_name and call["result_summary"] is None:
            call["result_summary"] = summary
            return
