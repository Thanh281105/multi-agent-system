"""Domain-owned deterministic plan compilers for the default agent catalog."""

from __future__ import annotations

from typing import Any

from app.contracts import ExecutionStep
from app.registry.registry import IntentManifest


def _step(
    step_id: str,
    agent_id: str,
    action: str,
    payload: dict[str, Any],
    *,
    depends_on: tuple[str, ...] = (),
) -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        agent_id=agent_id,
        action=action,
        depends_on=depends_on,
        input=payload,
    )


def _product_filters(entities: dict[str, Any]) -> dict[str, Any]:
    allowed = {
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
    return {
        key: value
        for key, value in entities.items()
        if key in allowed and value is not None
    }


def _product_search_input(entities: dict[str, Any]) -> dict[str, Any]:
    payload = _product_filters(entities)
    product_query = entities.get("product_query")
    if (
        "category" not in payload
        and "author" not in payload
        and "publisher" not in payload
        and isinstance(product_query, str)
    ):
        payload["query"] = product_query
    return payload


def _empty_plan(_: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    return ()


def _product_search(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    return (
        _step(
            "step_product",
            "product_agent",
            "product.search",
            _product_search_input(entities),
        ),
    )


def _product_rank(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    return (
        _step(
            "step_product",
            "product_agent",
            "product.rank",
            _product_search_input(entities),
        ),
    )


def _product_follow_up(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    product_id = entities.get("product_id")
    payload = {"product_ids": [product_id]} if product_id else entities
    return (_step("step_product", "product_agent", "product.compare", payload),)


def _product_compare(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    queries = entities.get("product_queries", [])
    if not isinstance(queries, list) or len(queries) < 2:
        return (
            _step(
                "step_compare",
                "product_agent",
                "product.compare",
                {"product_ids": entities.get("product_ids", [])},
            ),
        )

    dependencies: list[str] = []
    steps: list[ExecutionStep] = []
    for index, query in enumerate(queries, start=1):
        step_id = f"step_resolve_{index}"
        dependencies.append(step_id)
        steps.append(
            _step(
                step_id,
                "product_agent",
                "product.search",
                {"query": query, "limit": 1},
            )
        )
    steps.append(
        _step(
            "step_compare",
            "product_agent",
            "product.compare",
            {"product_ids_from": dependencies},
            depends_on=tuple(dependencies),
        )
    )
    return tuple(steps)


def _product_dependent(
    entities: dict[str, Any],
    *,
    target: str,
    action: str,
) -> tuple[ExecutionStep, ...]:
    product_id = entities.get("product_id")
    if isinstance(product_id, int):
        return (_step(f"step_{target}", target, action, {"product_id": product_id}),)

    product_input = _product_search_input(entities)
    if not product_input:
        raw_query = entities.get("query")
        if isinstance(raw_query, str) and raw_query.strip():
            product_input["query"] = raw_query
    product_input["limit"] = 1
    return (
        _step(
            "step_product",
            "product_agent",
            "product.search",
            product_input,
        ),
        _step(
            f"step_{target}",
            target,
            action,
            {"product_id_from": "step_product"},
            depends_on=("step_product",),
        ),
    )


def _review_summary(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    return _product_dependent(
        entities,
        target="review_agent",
        action="review.summarize",
    )


def _trust_complaints(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    return _product_dependent(
        entities,
        target="trust_agent",
        action="trust.complaints",
    )


def _multi_recommendation(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    product_input = _product_search_input(entities)
    product_input.setdefault("limit", 5)
    return (
        _step("step_product", "product_agent", "product.rank", product_input),
        _step(
            "step_review",
            "review_agent",
            "review.compare",
            {"product_ids_all_from": "step_product"},
            depends_on=("step_product",),
        ),
        _step(
            "step_trust",
            "trust_agent",
            "trust.compare",
            {"product_ids_all_from": "step_product"},
            depends_on=("step_product",),
        ),
    )


def _market_analyze(entities: dict[str, Any]) -> tuple[ExecutionStep, ...]:
    return (_step("step_market", "market_agent", "market.analyze", entities),)


def _review_expected(entities: dict[str, Any]) -> tuple[str, ...]:
    if isinstance(entities.get("product_id"), int):
        return ("review.summarize",)
    return ("product.search", "review.summarize")


def _trust_expected(entities: dict[str, Any]) -> tuple[str, ...]:
    if isinstance(entities.get("product_id"), int):
        return ("trust.complaints",)
    return ("product.search", "trust.complaints")


def _product_compare_expected(entities: dict[str, Any]) -> tuple[str, ...]:
    queries = entities.get("product_queries")
    if isinstance(queries, list) and len(queries) >= 2:
        return ("product.search", "product.compare")
    return ("product.compare",)


def build_default_intent_manifests() -> tuple[IntentManifest, ...]:
    """Return the catalog's complete domain routing and plan declarations."""

    return (
        IntentManifest("general.help", None, _empty_plan, lambda _: ()),
        IntentManifest("general.unsupported", None, _empty_plan, lambda _: ()),
        IntentManifest(
            "product.search",
            "product_agent",
            _product_search,
            lambda _: ("product.search",),
        ),
        IntentManifest(
            "product.rank",
            "product_agent",
            _product_rank,
            lambda _: ("product.rank",),
        ),
        IntentManifest(
            "product.follow_up",
            "product_agent",
            _product_follow_up,
            lambda _: ("product.compare",),
        ),
        IntentManifest(
            "product.compare",
            "product_agent",
            _product_compare,
            _product_compare_expected,
        ),
        IntentManifest(
            "review.summary",
            "review_agent",
            _review_summary,
            _review_expected,
        ),
        IntentManifest(
            "trust.complaints",
            "trust_agent",
            _trust_complaints,
            _trust_expected,
        ),
        IntentManifest(
            "multi.recommendation",
            "product_agent",
            _multi_recommendation,
            lambda _: ("product.rank", "review.compare", "trust.compare"),
        ),
        IntentManifest(
            "market.analyze",
            "market_agent",
            _market_analyze,
            lambda _: ("market.analyze",),
        ),
    )
