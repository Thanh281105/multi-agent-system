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
        {"review.get", "review.analyze", "review.summarize"}
    )

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
                    "review.product_id_required",
                    "Review Agent cần product_id hợp lệ.",
                ),
                started_at=started_at,
            )

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
            return self.failed(
                message,
                self.gateway_error(retrieval),
                started_at=started_at,
            )
        if message.action == "review.get" or not retrieval.data.get("found"):
            return self._result(
                message,
                data={"retrieval": retrieval.data},
                started_at=started_at,
            )

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
        if message.action == "review.summarize" and not errors:
            data["summary"] = self._summary(data)
        return self._result(
            message,
            data=data,
            started_at=started_at,
            errors=tuple(errors),
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
            provenance=(
                DataProvenance(
                    source_type="sample.database",
                    source_id="postgresql:reviews",
                    fields=("rating", "content", "created_at"),
                    sample_data=True,
                ),
            ),
            duration_ms=(perf_counter() - started_at) * 1_000,
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
