"""Build validated execution plans from routed intents."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.contracts import ExecutionPlan, ExecutionStep
from app.orchestrator.schemas import RoutedIntent


class ExecutionPlanner:
    """Create small DAGs whose bindings are resolved by the executor."""

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
                    "review.summarize",
                    {"product_id_from": "step_product"},
                    depends_on=("step_product",),
                )
            )
            steps.append(
                self._step(
                    "step_trust",
                    "trust_agent",
                    "trust.analyze",
                    {"product_id_from": "step_product"},
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
