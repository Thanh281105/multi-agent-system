"""Common execution helpers for typed domain agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from time import perf_counter
from typing import Any

from app.agent_gateway import AgentGateway, GatewayRequest, GatewayResponse
from app.contracts import AgentError, AgentMessage, AgentResult, TaskStatus


class DomainAgent(ABC):
    """Base class that enforces target/action validation and gateway-only tools."""

    agent_id: str
    supported_actions: frozenset[str]

    def __init__(self, gateway: AgentGateway) -> None:
        self.gateway = gateway

    @abstractmethod
    async def execute(self, message: AgentMessage) -> AgentResult:
        """Execute one typed A2A task."""

    def validate_message(self, message: AgentMessage) -> AgentError | None:
        if message.target != self.agent_id:
            return self.error(
                "agent.wrong_target",
                f"Task target {message.target} không khớp {self.agent_id}.",
            )
        if message.action not in self.supported_actions:
            return self.error(
                "agent.unsupported_action",
                f"Action {message.action} không được agent hỗ trợ.",
            )
        return None

    async def call_tool(
        self,
        message: AgentMessage,
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> GatewayResponse:
        return await self.gateway.execute(
            GatewayRequest(
                agent_id=self.agent_id,
                task_id=message.task_id,
                request_id=message.request_id,
                trace_id=message.trace_id,
                server_id=server_id,
                tool_name=tool_name,
                arguments=arguments,
            )
        )

    def failed(
        self,
        message: AgentMessage,
        error: AgentError,
        *,
        started_at: float,
        data: dict[str, Any] | None = None,
    ) -> AgentResult:
        return AgentResult(
            task_id=message.task_id,
            agent_id=self.agent_id,
            status=TaskStatus.FAILED,
            data=data or {},
            errors=(error,),
            duration_ms=(perf_counter() - started_at) * 1_000,
        )

    def error(self, code: str, message: str, *, retryable: bool = False) -> AgentError:
        return AgentError(
            code=code,
            message=message,
            source=self.agent_id,
            retryable=retryable,
        )

    def gateway_error(self, response: GatewayResponse) -> AgentError:
        return response.error or self.error(
            "agent.gateway_failed",
            "Agent Gateway không trả lỗi chuẩn hóa.",
            retryable=True,
        )
