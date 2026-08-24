"""Review intelligence agent with deterministic Vietnamese analysis."""

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


class ReviewAgent(DomainAgent):
    """Retrieve reviews and compute sentiment/aspect summaries."""

    agent_id = "review_agent"
    supported_actions = frozenset(
        {"review.get", "review.analyze", "review.summarize", "review.compare"}
    )

    def __init__(self, gateway: AgentGateway) -> None:
        super().__init__(gateway)

    async def execute(self, message: AgentMessage) -> AgentResult:
        started_at = perf_counter()
        validation_error = self.validate_message(message)
        if validation_error:
            return self.failed(message, validation_error, started_at=started_at)

        if message.action == "review.compare":
            product_ids = self._product_ids(message.payload.get("product_ids"))
            if product_ids is None:
                return self.failed(
                    message,
                    self.error(
                        "review.product_ids_required",
                        "Review Agent cần từ 1 đến 5 product_id không trùng nhau.",
                    ),
                    started_at=started_at,
                )
            analyses = await asyncio.gather(
                *(
                    self._analyze_product(message, product_id)
                    for product_id in product_ids
                )
            )
            return self._batch_result(
                message,
                product_ids=product_ids,
                analyses=analyses,
                started_at=started_at,
            )

        product_id = message.payload.get("product_id")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            return self.failed(
                message,
                self.error(
                    "review.product_id_required",
                    "Review Agent cần product_id hợp lệ.",
                ),
                started_at=started_at,
            )

        data, errors = await self._analyze_product(
            message,
            product_id,
            retrieve_only=message.action == "review.get",
            include_summary=message.action == "review.summarize",
        )
        if not data and errors:
            return self.failed(
                message,
                errors[0],
                started_at=started_at,
            )
        return self._result(
            message,
            data=data,
            started_at=started_at,
            errors=errors,
        )

    async def _analyze_product(
        self,
        message: AgentMessage,
        product_id: int,
        *,
        retrieve_only: bool = False,
        include_summary: bool = False,
    ) -> tuple[dict[str, Any], tuple[AgentError, ...]]:
        """Retrieve and analyze one product without constructing an A2A result."""

        retrieval = await self.call_tool(
            message,
            server_id="review_db",
            tool_name="get_product_reviews",
            arguments={
                "product_id": product_id,
                "limit": message.payload.get("limit", 20),
            },
        )
        if not retrieval.ok:
            return {}, (self.gateway_error(retrieval),)
        if retrieve_only or not retrieval.data.get("found"):
            return {"retrieval": retrieval.data}, ()

        reviews = retrieval.data.get("reviews", [])
        sentiment, aspects = await asyncio.gather(
            self.call_tool(
                message,
                server_id="analytics",
                tool_name="analyze_review_sentiment",
                arguments={"reviews": reviews},
            ),
            self.call_tool(
                message,
                server_id="analytics",
                tool_name="extract_review_aspects",
                arguments={"reviews": reviews},
            ),
        )
        data: dict[str, Any] = {"retrieval": retrieval.data}
        errors: list[AgentError] = []
        if sentiment.ok:
            data["sentiment"] = sentiment.data
        else:
            errors.append(self.gateway_error(sentiment))
        if aspects.ok:
            data["aspects"] = aspects.data
        else:
            errors.append(self.gateway_error(aspects))
        if include_summary and not errors:
            data["summary"] = self._summary(data)
        return data, tuple(errors)

    def _batch_result(
        self,
        message: AgentMessage,
        *,
        product_ids: tuple[int, ...],
        analyses: list[tuple[dict[str, Any], tuple[AgentError, ...]]],
        started_at: float,
    ) -> AgentResult:
        errors = tuple(error for _, item_errors in analyses for error in item_errors)
        items = [
            {
                "product_id": product_id,
                "status": (
                    TaskStatus.FAILED.value
                    if not data and item_errors
                    else (
                        TaskStatus.PARTIAL_SUCCESS.value
                        if item_errors
                        else TaskStatus.SUCCESS.value
                    )
                ),
                "error_codes": [error.code for error in item_errors],
                **data,
            }
            for product_id, (data, item_errors) in zip(
                product_ids,
                analyses,
                strict=True,
            )
        ]
        if all(item["status"] == TaskStatus.FAILED.value for item in items):
            status = TaskStatus.FAILED
        elif errors:
            status = TaskStatus.PARTIAL_SUCCESS
        else:
            status = TaskStatus.SUCCESS
        return AgentResult(
            task_id=message.task_id,
            agent_id=self.agent_id,
            status=status,
            data={
                "requested_product_ids": list(product_ids),
                "analyses": items,
                "method": "per_product_sentiment_aspects_vi_v1",
            },
            errors=errors,
            provenance=self.provenance_or_fallback(
                {
                    "analyses": items,
                },
                self._provenance(),
            ),
            duration_ms=(perf_counter() - started_at) * 1_000,
        )

    def _result(
        self,
        message: AgentMessage,
        *,
        data: dict[str, Any],
        started_at: float,
        errors: tuple[AgentError, ...] = (),
    ) -> AgentResult:
        return AgentResult(
            task_id=message.task_id,
            agent_id=self.agent_id,
            status=TaskStatus.PARTIAL_SUCCESS if errors else TaskStatus.SUCCESS,
            data=data,
            errors=errors,
            provenance=self.provenance_or_fallback(data, self._provenance()),
            duration_ms=(perf_counter() - started_at) * 1_000,
        )

    @staticmethod
    def _product_ids(value: object) -> tuple[int, ...] | None:
        if not isinstance(value, list) or not 1 <= len(value) <= 5:
            return None
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1
            for item in value
        ):
            return None
        product_ids = tuple(int(item) for item in value)
        return product_ids if len(product_ids) == len(set(product_ids)) else None

    @staticmethod
    def _provenance() -> DataProvenance:
        return DataProvenance(
            source_type="sample.database",
            source_id="postgresql:reviews",
            fields=("rating", "content", "created_at"),
            sample_data=True,
        )

    @staticmethod
    def _summary(data: dict[str, Any]) -> str:
        distribution = data["sentiment"]["distribution"]
        negative_aspects = data["aspects"]["negative_aspects"]
        positive_percent = round(float(distribution["positive"]) * 100)
        if negative_aspects:
            issues = ", ".join(negative_aspects[:3])
            return (
                f"{positive_percent}% review được phân loại tích cực; "
                f"các khía cạnh có tín hiệu chưa tốt gồm {issues}."
            )
        return (
            f"{positive_percent}% review được phân loại tích cực; "
            "chưa phát hiện khía cạnh tiêu cực nổi bật trong dữ liệu mẫu."
        )
