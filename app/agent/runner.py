"""OpenAI Responses API runner with an explicit database-tool loop."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI

from app.agent.agent import root_agent
from app.core.config import settings
from app.schemas.chat import ToolCallInfo

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
openai_client: AsyncOpenAI | None = None
session_response_ids: dict[str, str] = {}
runner_lock = asyncio.Lock()


class AgentRunError(RuntimeError):
    """Raised when OpenAI or the agent tool loop cannot complete a turn."""


@dataclass(frozen=True)
class AgentRunResult:
    """API-neutral result produced after collecting safe tool-call metadata."""

    answer: str
    tool_calls: list[ToolCallInfo]


async def run_agent(
    *,
    message: str,
    session_id: str,
    request_id: str,
    user_id: str = "api-user",
) -> AgentRunResult:
    """Run one user turn and execute any model-requested database tools."""

    del user_id
    started_at = perf_counter()
    logger.info(
        "AGENT_START request_id=%s model=%s",
        request_id,
        settings.openai_model,
    )

    async with runner_lock:
        try:
            client = _get_client()
            previous_response_id = session_response_ids.get(session_id)
            response = await _create_response(
                client=client,
                input_items=message,
                previous_response_id=previous_response_id,
            )
            tool_calls: list[dict[str, Any]] = []

            for _ in range(MAX_TOOL_ROUNDS):
                function_calls = _get_function_calls(response)
                if not function_calls:
                    answer = str(getattr(response, "output_text", "") or "").strip()
                    if not answer:
                        raise AgentRunError("OpenAI returned no final text response")
                    response_id = _get_response_id(response)
                    session_response_ids[session_id] = response_id
                    result = AgentRunResult(
                        answer=answer,
                        tool_calls=[
                            ToolCallInfo.model_validate(call) for call in tool_calls
                        ],
                    )
                    logger.info(
                        "AGENT_COMPLETE request_id=%s tool_calls=%d latency_ms=%d",
                        request_id,
                        len(result.tool_calls),
                        int((perf_counter() - started_at) * 1000),
                    )
                    return result

                tool_outputs: list[dict[str, Any]] = []
                for function_call in function_calls:
                    name = str(getattr(function_call, "name", "unknown_tool"))
                    call_info: dict[str, Any] = {
                        "name": name,
                        "arguments": {},
                        "result_summary": None,
                    }
                    tool_calls.append(call_info)
                    logger.info(
                        "OPENAI_TOOL_CALL request_id=%s tool=%s",
                        request_id,
                        name,
                    )

                    try:
                        arguments = _parse_arguments(
                            getattr(function_call, "arguments", "{}")
                        )
                        call_info["arguments"] = arguments
                        tool_result = await _execute_tool(
                            name=name,
                            arguments=arguments,
                            request_id=request_id,
                            session_id=session_id,
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        logger.error(
                            "OPENAI_TOOL_ARGUMENT_ERROR request_id=%s tool=%s",
                            request_id,
                            name,
                        )
                        tool_result = {"error": "Tool arguments không hợp lệ."}

                    call_info["result_summary"] = _summarize_tool_result(tool_result)
                    call_id = getattr(function_call, "call_id", None)
                    if not call_id:
                        raise AgentRunError(
                            "OpenAI returned a tool call without call_id"
                        )
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(
                                tool_result,
                                ensure_ascii=False,
                                default=str,
                            ),
                        }
                    )

                response = await _create_response(
                    client=client,
                    input_items=tool_outputs,
                    previous_response_id=_get_response_id(response),
                )

            raise AgentRunError("OpenAI exceeded the maximum tool-call rounds")
        except AgentRunError:
            logger.error(
                "AGENT_ERROR request_id=%s latency_ms=%d",
                request_id,
                int((perf_counter() - started_at) * 1000),
            )
            raise
        except Exception as exc:
            logger.error(
                "AGENT_ERROR request_id=%s latency_ms=%d",
                request_id,
                int((perf_counter() - started_at) * 1000),
            )
            raise AgentRunError("OpenAI runtime failed") from exc


def _get_client() -> AsyncOpenAI:
    if openai_client is not None:
        return openai_client
    if not settings.openai_api_key_value:
        raise AgentRunError("OPENAI_API_KEY is not configured")
    return _create_client()


def _create_client() -> AsyncOpenAI:
    global openai_client
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key_value)
    return openai_client


async def _create_response(
    *,
    client: AsyncOpenAI,
    input_items: Any,
    previous_response_id: str | None,
) -> Any:
    request: dict[str, Any] = {
        "model": settings.openai_model,
        "instructions": root_agent.instructions,
        "input": input_items,
        "tools": list(root_agent.tools),
        "store": True,
    }
    if previous_response_id:
        request["previous_response_id"] = previous_response_id
    return await client.responses.create(**request)


def _get_function_calls(response: Any) -> list[Any]:
    return [
        item
        for item in (getattr(response, "output", None) or [])
        if getattr(item, "type", None) == "function_call"
    ]


def _get_response_id(response: Any) -> str:
    response_id = getattr(response, "id", None)
    if not response_id:
        raise AgentRunError("OpenAI returned a response without an id")
    return str(response_id)


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        raise TypeError("tool arguments must be a JSON object")
    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


async def _execute_tool(
    *,
    name: str,
    arguments: dict[str, Any],
    request_id: str,
    session_id: str,
) -> dict[str, Any]:
    tool = root_agent.functions.get(name)
    if tool is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        result = await asyncio.to_thread(tool, **arguments)
        if not isinstance(result, dict):
            raise TypeError("tool result must be a dictionary")
        logger.info(
            "TOOL_RESULT request_id=%s tool=%s summary=%s",
            request_id,
            name,
            _summarize_tool_result(result),
        )
        return result
    except Exception:
        logger.error(
            "TOOL_ERROR request_id=%s tool=%s",
            request_id,
            name,
        )
        return {"error": "Không thể truy xuất dữ liệu từ tool."}


def _summarize_tool_result(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None

    summary: dict[str, Any] = {}
    if "error" in response:
        summary["error"] = True
    for key in ("found", "count"):
        if key in response and isinstance(response[key], (bool, int, float, str)):
            summary[key] = response[key]
    for key in ("products", "reviews", "missing_product_ids"):
        value = response.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    return summary or None
