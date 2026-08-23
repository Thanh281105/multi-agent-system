"""Trust and complaint agent with explicit rule-based evidence."""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any

from app.agent_gateway import AgentGateway
from app.agents.base import DomainAgent
from app.contracts import (
    AgentError,
    AgentMessage,
    AgentResult,
    DataProvenance,
    TaskStatus,
)


class TrustAgent(DomainAgent):
    """Detect spam-like patterns and complaint signals over factual reviews."""

    agent_id = "trust_agent"
    supported_actions = frozenset({"trust.analyze", "trust.complaints"})

    def __init__(self, gateway: AgentGateway) -> None:
        super().__init__(gateway)

    async def execute(self, message: AgentMessage) -> AgentResult:
        started_at = perf_counter()
        validation_error = self.validate_message(message)
        if validation_error:
            return self.failed(message, validation_error, started_at=started_at)
        product_id = message.payload.get("product_id")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            return self.failed(
                message,
                self.error(
                    "trust.product_id_required",
                    "Trust Agent cần product_id hợp lệ.",
                ),
                started_at=started_at,
            )

        retrieval = await self.call_tool(
            message,
            server_id="review_db",
            tool_name="get_product_reviews",
            arguments={
                "product_id": product_id,
                "limit": message.payload.get("limit", 50),
            },
        )
        if not retrieval.ok:
            return self.failed(
                message,
                self.gateway_error(retrieval),
                started_at=started_at,
            )
        if not retrieval.data.get("found"):
            return self._result(
                message,
                {"retrieval": retrieval.data},
                started_at,
            )

        reviews = retrieval.data.get("reviews", [])
        trust_call, complaint_call = await asyncio.gather(
            self.call_tool(
                message,
                server_id="analytics",
                tool_name="analyze_review_trust",
                arguments={"reviews": reviews},
            ),
            self.call_tool(
                message,
                server_id="analytics",
                tool_name="detect_complaints",
                arguments={"reviews": reviews},
            ),
        )
        data: dict[str, Any] = {"retrieval": retrieval.data}
        errors: list[AgentError] = []
        if trust_call.ok:
            data["trust"] = trust_call.data
        else:
            errors.append(self.gateway_error(trust_call))
        if complaint_call.ok:
            data["complaints"] = complaint_call.data
        else:
            errors.append(self.gateway_error(complaint_call))
        return self._result(
            message,
            data,
            started_at,
            errors=tuple(errors),
        )

    def _result(
        self,
        message: AgentMessage,
        data: dict[str, Any],
        started_at: float,
        *,
        errors: tuple[AgentError, ...] = (),
    ) -> AgentResult:
        return AgentResult(
            task_id=message.task_id,
            agent_id=self.agent_id,
            status=TaskStatus.PARTIAL_SUCCESS if errors else TaskStatus.SUCCESS,
            data=data,
            errors=errors,
            provenance=(
                DataProvenance(
                    source_type="sample.database",
                    source_id="postgresql:reviews",
                    fields=("rating", "content"),
                    sample_data=True,
                ),
            ),
            duration_ms=(perf_counter() - started_at) * 1_000,
        )
