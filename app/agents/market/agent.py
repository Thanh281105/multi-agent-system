"""Market intelligence agent over historical book-snapshot aggregates."""

from __future__ import annotations

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
    """Answer cross-sectional snapshot questions without trend claims."""

    agent_id = "market_agent"
    supported_actions = frozenset({"market.analyze", "market.search"})

    def __init__(self, gateway: AgentGateway) -> None:
        super().__init__(gateway)

    async def execute(self, message: AgentMessage) -> AgentResult:
        started_at = perf_counter()
        validation_error = self.validate_message(message)
        if validation_error:
            return self.failed(message, validation_error, started_at=started_at)

        if message.action == "market.search":
            query = str(message.payload.get("query", "sách Tiki"))
            knowledge = await self.call_tool(
                message,
                server_id="knowledge",
                tool_name="search_market_knowledge",
                arguments={"query": query, "limit": message.payload.get("limit", 3)},
            )
            return self._complete(
                message,
                {"knowledge": knowledge.data} if knowledge.ok else {},
                started_at,
                errors=(() if knowledge.ok else (self.gateway_error(knowledge),)),
            )

        allowed_filters = {
            "category",
            "author",
            "publisher",
            "min_price",
            "max_price",
            "min_rating",
        }
        market = await self.call_tool(
            message,
            server_id="analytics",
            tool_name="analyze_market",
            arguments={
                key: value
                for key, value in message.payload.items()
                if key in allowed_filters and value is not None
            },
        )
        data: dict[str, Any] = {}
        errors: list[AgentError] = []
        if market.ok:
            data["market"] = market.data
        else:
            errors.append(self.gateway_error(market))
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
                        fields=(
                            "category",
                            "authors",
                            "publisher",
                            "price",
                            "rating",
                        ),
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
