"""Market intelligence agent over sample aggregates and knowledge notes."""

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


class MarketAgent(DomainAgent):
    """Answer market-level questions without overstating sample evidence."""

    agent_id = "market_agent"
    supported_actions = frozenset({"market.analyze", "market.search"})

    def __init__(self, gateway: AgentGateway) -> None:
        super().__init__(gateway)

    async def execute(self, message: AgentMessage) -> AgentResult:
        started_at = perf_counter()
        validation_error = self.validate_message(message)
        if validation_error:
            return self.failed(message, validation_error, started_at=started_at)

        query = str(message.payload.get("query", "thị trường ecommerce Việt Nam"))
        knowledge_call = self.call_tool(
            message,
            server_id="knowledge",
            tool_name="search_market_knowledge",
            arguments={"query": query, "limit": message.payload.get("limit", 3)},
        )
        if message.action == "market.search":
            knowledge = await knowledge_call
            return self._complete(
                message,
                {"knowledge": knowledge.data} if knowledge.ok else {},
                started_at,
                errors=(() if knowledge.ok else (self.gateway_error(knowledge),)),
            )

        market, knowledge = await asyncio.gather(
            self.call_tool(
                message,
                server_id="analytics",
                tool_name="analyze_market",
                arguments={"category": message.payload.get("category")},
            ),
            knowledge_call,
        )
        data: dict[str, Any] = {}
        errors: list[AgentError] = []
        if market.ok:
            data["market"] = market.data
        else:
            errors.append(self.gateway_error(market))
        if knowledge.ok:
            data["knowledge"] = knowledge.data
        else:
            errors.append(self.gateway_error(knowledge))
        return self._complete(
            message,
            data,
            started_at,
            errors=tuple(errors),
        )

    def _complete(
        self,
        message: AgentMessage,
        data: dict[str, Any],
        started_at: float,
        *,
        errors: tuple[AgentError, ...] = (),
    ) -> AgentResult:
        status = TaskStatus.SUCCESS
        if errors and data:
            status = TaskStatus.PARTIAL_SUCCESS
        elif errors:
            status = TaskStatus.FAILED
        provenance: list[DataProvenance] = []
        if "market" in data:
            provenance.extend(
                self.provenance_or_fallback(
                    data["market"],
                    DataProvenance(
                        source_type="sample.database",
                        source_id="postgresql:products",
                        fields=("price", "rating", "sold_count"),
                        sample_data=True,
                    ),
                )
            )
        if "knowledge" in data:
            provenance.append(
                DataProvenance(
                    source_type="sample.document",
                    source_id="sample_thesis_market_notes",
                    fields=("title", "content"),
                    sample_data=True,
                )
            )
        return AgentResult(
            task_id=message.task_id,
            agent_id=self.agent_id,
            status=status,
            data=data,
            errors=errors,
            provenance=tuple(provenance),
            duration_ms=(perf_counter() - started_at) * 1_000,
        )
