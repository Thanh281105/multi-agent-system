"""Build validated execution plans from routed intents."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from app.contracts import ExecutionPlan, ExecutionStep
from app.orchestrator.model_schemas import CapabilityName, PlanningDecision
from app.orchestrator.schemas import RoutedIntent
from app.shared import (
    ModelRuntime,
    ModelRuntimeError,
    ModelRuntimeMode,
    ReasoningEffort,
    mark_model_call_fallback,
)


class ExecutionPlanner:
    """Create small DAGs whose bindings are resolved by the executor."""

    def __init__(
        self,
        *,
        model_runtime: ModelRuntime | None = None,
        runtime_mode: ModelRuntimeMode = "off",
        model: str = "gpt-5.4-mini",
        reasoning_effort: ReasoningEffort = "low",
    ) -> None:
        self.model_runtime = model_runtime
        self.runtime_mode = runtime_mode
        self.model = model
        self.reasoning_effort = reasoning_effort

    def build(self, routed: RoutedIntent) -> ExecutionPlan:
        intent = routed.intent
        entities = dict(routed.entities)
        steps: list[ExecutionStep] = []

        if intent in {"general.help", "general.unsupported"}:
            return self._plan(intent, steps)
        if intent == "product.search":
            steps.append(
                self._step(
                    "step_product",
                    "product_agent",
                    intent,
                    self._product_search_input(entities),
                )
            )
        elif intent == "product.rank":
            steps.append(
                self._step(
                    "step_product",
                    "product_agent",
                    intent,
                    self._product_search_input(entities),
                )
            )
        elif intent == "product.follow_up":
            product_id = entities.get("product_id")
            payload = {"product_ids": [product_id]} if product_id else entities
            steps.append(
                self._step(
                    "step_product",
                    "product_agent",
                    "product.compare",
                    payload,
                )
            )
        elif intent == "product.compare":
            queries = entities.get("product_queries", [])
            if isinstance(queries, list) and len(queries) >= 2:
                dependencies = []
                for index, query in enumerate(queries, start=1):
                    step_id = f"step_resolve_{index}"
                    dependencies.append(step_id)
                    steps.append(
                        self._step(
                            step_id,
                            "product_agent",
                            "product.search",
                            {"query": query, "limit": 1},
                        )
                    )
                steps.append(
                    self._step(
                        "step_compare",
                        "product_agent",
                        "product.compare",
                        {"product_ids_from": dependencies},
                        depends_on=dependencies,
                    )
                )
            else:
                steps.append(
                    self._step(
                        "step_compare",
                        "product_agent",
                        "product.compare",
                        {"product_ids": entities.get("product_ids", [])},
                    )
                )
        elif intent in {"review.summary", "trust.complaints"}:
            target = "review_agent" if intent.startswith("review") else "trust_agent"
            action = "review.summarize" if target == "review_agent" else intent
            steps.extend(self._product_dependent_steps(entities, target, action))
        elif intent == "multi.recommendation":
            product_input = self._product_filters(entities)
            product_input.setdefault("limit", 5)
            steps.append(
                self._step(
                    "step_product",
                    "product_agent",
                    "product.rank",
                    product_input,
                )
            )
            steps.append(
                self._step(
                    "step_review",
                    "review_agent",
                    "review.compare",
                    {"product_ids_all_from": "step_product"},
                    depends_on=("step_product",),
                )
            )
            steps.append(
                self._step(
                    "step_trust",
                    "trust_agent",
                    "trust.compare",
                    {"product_ids_all_from": "step_product"},
                    depends_on=("step_product",),
                )
            )
        elif intent == "market.analyze":
            steps.append(
                self._step(
                    "step_market",
                    "market_agent",
                    "market.analyze",
                    entities,
                )
            )
        elif intent == "market.search":
            steps.append(
                self._step(
                    "step_market",
                    "market_agent",
                    "market.search",
                    entities,
                )
            )
        else:
            return self._plan("general.unsupported", [])
        return self._plan(intent, steps)

    async def build_async(self, routed: RoutedIntent) -> ExecutionPlan:
        """Ask the model for capabilities, then compile an authorized DAG."""

        fallback = self.build(routed)
        expected = self._expected_capabilities(routed)
        if (
            self.model_runtime is None
            or self.runtime_mode == "off"
            or not expected
        ):
            return fallback

        try:
            result = await self.model_runtime.generate_structured(
                stage="planning",
                agent_id="orchestrator",
                model=self.model,
                instructions=(
                    "Bạn là planner cho hệ thống multi-agent. Chọn tập capability "
                    "nhỏ nhất, đúng thứ tự phụ thuộc để xử lý intent. Chỉ dùng "
                    "capability trong schema; không tạo MCP tool, server, action, "
                    "agent hay dữ liệu đầu vào mới. rationale là lý do ngắn, không "
                    "phải chuỗi suy luận."
                ),
                input_text=json.dumps(
                    {
                        "route": routed.model_dump(mode="json"),
                        "policy": {
                            "max_steps": 8,
                            "max_candidates": 5,
                            "tool_access": "agent_gateway_only",
                        },
                    },
                    ensure_ascii=False,
                ),
                schema=PlanningDecision,
                max_output_tokens=500,
                reasoning_effort=self.reasoning_effort,
            )
        except ModelRuntimeError as exc:
            if self.runtime_mode == "required":
                raise
            mark_model_call_fallback(exc.metadata, "deterministic_planning")
            return fallback

        if self.runtime_mode == "shadow":
            mark_model_call_fallback(result.metadata, "shadow_mode")
            return fallback

        proposed = tuple(result.value.capabilities)
        if proposed != expected:
            mark_model_call_fallback(result.metadata, "unauthorized_plan")
            if self.runtime_mode == "required":
                raise ValueError("model_plan_not_authorized")
            return fallback

        entities = dict(routed.entities)
        if routed.intent in {"product.rank", "multi.recommendation"}:
            entities["limit"] = result.value.candidate_limit
        compiled_route = routed.model_copy(update={"entities": entities})
        compiled = self.build(compiled_route)
        if len(compiled.steps) > 8:
            mark_model_call_fallback(result.metadata, "plan_step_limit")
            if self.runtime_mode == "required":
                raise ValueError("model_plan_exceeds_step_limit")
            return fallback
        return compiled

    @staticmethod
    def _expected_capabilities(
        routed: RoutedIntent,
    ) -> tuple[CapabilityName, ...]:
        intent = routed.intent
        if intent in {"general.help", "general.unsupported"}:
            return ()
        if intent == "product.search":
            return ("product.search",)
        if intent == "product.rank":
            return ("product.rank",)
        if intent == "product.follow_up":
            return ("product.compare",)
        if intent == "product.compare":
            queries = routed.entities.get("product_queries")
            if isinstance(queries, list) and len(queries) >= 2:
                return ("product.search", "product.compare")
            return ("product.compare",)
        if intent == "review.summary":
            if isinstance(routed.entities.get("product_id"), int):
                return ("review.summarize",)
            return ("product.search", "review.summarize")
        if intent == "trust.complaints":
            if isinstance(routed.entities.get("product_id"), int):
                return ("trust.complaints",)
            return ("product.search", "trust.complaints")
        if intent == "multi.recommendation":
            return ("product.rank", "review.compare", "trust.compare")
        if intent == "market.analyze":
            return ("market.analyze",)
        if intent == "market.search":
            return ("market.search",)
        return ()

    def _product_dependent_steps(
        self,
        entities: dict[str, Any],
        target: str,
        action: str,
    ) -> list[ExecutionStep]:
        product_id = entities.get("product_id")
        if isinstance(product_id, int):
            return [
                self._step(
                    f"step_{target}",
                    target,
                    action,
                    {"product_id": product_id},
                )
            ]

        query = entities.get("product_query") or entities.get("query")
        return [
            self._step(
                "step_product",
                "product_agent",
                "product.search",
                {"query": query, "limit": 1},
            ),
            self._step(
                f"step_{target}",
                target,
                action,
                {"product_id_from": "step_product"},
                depends_on=("step_product",),
            ),
        ]

    @staticmethod
    def _product_filters(entities: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "category",
            "max_price",
            "min_price",
            "min_rating",
            "platform",
            "limit",
        }
        return {
            key: value
            for key, value in entities.items()
            if key in allowed and value is not None
        }

    @classmethod
    def _product_search_input(cls, entities: dict[str, Any]) -> dict[str, Any]:
        payload = cls._product_filters(entities)
        product_query = entities.get("product_query")
        if "category" not in payload and isinstance(product_query, str):
            payload["query"] = product_query
        return payload

    @staticmethod
    def _step(
        step_id: str,
        agent_id: str,
        action: str,
        payload: dict[str, Any],
        *,
        depends_on: list[str] | tuple[str, ...] = (),
    ) -> ExecutionStep:
        return ExecutionStep(
            step_id=step_id,
            agent_id=agent_id,
            action=action,
            depends_on=tuple(depends_on),
            input=payload,
        )

    @staticmethod
    def _plan(intent: str, steps: list[ExecutionStep]) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id=f"plan_{uuid4().hex}",
            intent=intent,
            steps=tuple(steps),
        )
