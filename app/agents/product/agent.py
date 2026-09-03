"""Book catalog agent backed exclusively by Agent Gateway skills."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.agent_gateway import AgentGateway, GatewayResponse
from app.agents.base import DomainAgent
from app.contracts import AgentMessage, AgentResult, DataProvenance, TaskStatus

SEARCH_ARGUMENTS = frozenset(
    {
        "query",
        "category",
        "max_price",
        "min_price",
        "min_rating",
        "author",
        "publisher",
        "min_page_count",
        "max_page_count",
        "limit",
    }
)


class ProductAgent(DomainAgent):
    """Search, compare, and rank historical Tiki book facts."""

    agent_id = "product_agent"
    supported_actions = frozenset(
        {
            "product.search",
            "product.compare",
            "product.rank",
            "product.statistics",
        }
    )

    def __init__(self, gateway: AgentGateway) -> None:
        super().__init__(gateway)

    async def execute(self, message: AgentMessage) -> AgentResult:
        started_at = perf_counter()
        validation_error = self.validate_message(message)
        if validation_error:
            return self.failed(message, validation_error, started_at=started_at)

        if message.action == "product.compare":
            response = await self.call_tool(
                message,
                server_id="product_db",
                tool_name="compare_products",
                arguments={"product_ids": message.payload.get("product_ids", [])},
            )
            return self._complete(message, response, started_at=started_at)

        if message.action == "product.statistics":
            response = await self.call_tool(
                message,
                server_id="analytics",
                tool_name="get_product_statistics",
                arguments={"category": message.payload.get("category")},
            )
            return self._complete(message, response, started_at=started_at)

        search_arguments = {
            key: value
            for key, value in message.payload.items()
            if key in SEARCH_ARGUMENTS and value is not None
        }
        search_response = await self.call_tool(
            message,
            server_id="product_db",
            tool_name="search_products",
            arguments=search_arguments,
        )
        if not search_response.ok:
            return self.failed(
                message,
                self.gateway_error(search_response),
                started_at=started_at,
            )
        if message.action == "product.search":
            return self._success(message, search_response.data, started_at)

        products = search_response.data.get("products", [])
        rank_response = await self.call_tool(
            message,
            server_id="analytics",
            tool_name="rank_products",
            arguments={"products": products},
        )
        if not rank_response.ok:
            return AgentResult(
                task_id=message.task_id,
                agent_id=self.agent_id,
                status=TaskStatus.PARTIAL_SUCCESS,
                data={"search": search_response.data},
                errors=(self.gateway_error(rank_response),),
                provenance=self.provenance_or_fallback(
                    search_response.data,
                    self._provenance(),
                ),
                duration_ms=(perf_counter() - started_at) * 1_000,
            )
        return self._success(
            message,
            {"search": search_response.data, "ranking": rank_response.data},
            started_at,
        )

    def _complete(
        self,
        message: AgentMessage,
        response: GatewayResponse,
        *,
        started_at: float,
    ) -> AgentResult:
        if not response.ok:
            return self.failed(
                message,
                self.gateway_error(response),
                started_at=started_at,
            )
        return self._success(message, response.data, started_at)

    def _success(
        self,
        message: AgentMessage,
        data: dict[str, Any],
        started_at: float,
    ) -> AgentResult:
        return AgentResult(
            task_id=message.task_id,
            agent_id=self.agent_id,
            status=TaskStatus.SUCCESS,
            data=data,
            provenance=self.provenance_or_fallback(data, self._provenance()),
            duration_ms=(perf_counter() - started_at) * 1_000,
        )

    @staticmethod
    def _provenance() -> DataProvenance:
        return DataProvenance(
            source_type="sample.database",
            source_id="postgresql:products",
            fields=(
                "name",
                "authors",
                "publisher",
                "category",
                "page_count",
                "price",
                "rating",
                "sold_count",
            ),
            sample_data=True,
        )
