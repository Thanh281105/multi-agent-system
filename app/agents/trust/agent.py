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
    supported_actions = frozenset(
        {"trust.analyze", "trust.complaints", "trust.compare"}
    )

    def __init__(self, gateway: AgentGateway) -> None:
        super().__init__(gateway)

    async def execute(self, message: AgentMessage) -> AgentResult:
        started_at = perf_counter()
        validation_error = self.validate_message(message)
        if validation_error:
            return self.failed(message, validation_error, started_at=started_at)

        if message.action == "trust.compare":
            product_ids = self._product_ids(message.payload.get("product_ids"))
            if product_ids is None:
                return self.failed(
                    message,
                    self.error(
                        "trust.product_ids_required",
                        "Trust Agent cần từ 1 đến 5 product_id không trùng nhau.",
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
                    "trust.product_id_required",
                    "Trust Agent cần product_id hợp lệ.",
                ),
                started_at=started_at,
            )

        data, errors = await self._analyze_product(message, product_id)
        if not data and errors:
            return self.failed(
                message,
                errors[0],
                started_at=started_at,
            )
        return self._result(
            message,
            data,
            started_at,
            errors=errors,
        )

    async def _analyze_product(
        self,
        message: AgentMessage,
        product_id: int,
    ) -> tuple[dict[str, Any], tuple[AgentError, ...]]:
        """Retrieve and score trust/complaints for one product."""

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
            return {}, (self.gateway_error(retrieval),)
        if not retrieval.data.get("found"):
            return {"retrieval": retrieval.data}, ()

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
                "method": "per_product_trust_complaints_vi_v1",
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
            fields=("rating", "content"),
            sample_data=True,
        )
