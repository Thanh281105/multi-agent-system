"""Transparent cross-agent scoring for recommendation candidates."""

from __future__ import annotations

from typing import Any

RECOMMENDATION_WEIGHTS: dict[str, float] = {
    "product_fit": 0.55,
    "positive_sentiment": 0.15,
    "complaint_safety": 0.20,
    "review_trust": 0.10,
}


def score_recommendation_candidates(
    products: list[dict[str, Any]],
    review_analyses: list[dict[str, Any]],
    trust_analyses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine available bounded signals and expose the complete score breakdown."""

    reviews_by_product = _index_analyses(review_analyses)
    trust_by_product = _index_analyses(trust_analyses)
    unscored: list[tuple[dict[str, Any], dict[str, float]]] = []

    for source_rank, product in enumerate(products, start=1):
        product_id = product.get("id")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            continue

        product_fit = _unit_interval(product.get("ranking_score"))
        if product_fit is None:
            continue
        components: dict[str, float] = {"product_fit": product_fit}

        review = reviews_by_product.get(product_id, {})
        sentiment = review.get("sentiment")
        if isinstance(sentiment, dict):
            distribution = sentiment.get("distribution")
            if isinstance(distribution, dict):
                positive = _unit_interval(distribution.get("positive"))
                if positive is not None:
                    components["positive_sentiment"] = positive

        trust = trust_by_product.get(product_id, {})
        complaints = trust.get("complaints")
        if isinstance(complaints, dict):
            complaint_rate = _unit_interval(complaints.get("complaint_rate"))
            if complaint_rate is not None:
                components["complaint_safety"] = 1 - complaint_rate
        trust_score = trust.get("trust")
        if isinstance(trust_score, dict):
            average_trust = _unit_interval(
                trust_score.get("average_trust_score")
            )
            if average_trust is not None:
                components["review_trust"] = average_trust

        unscored.append(
            (
                {
                    **product,
                    "source_rank": source_rank,
                    "complaint_rate": _nested_unit(
                        trust,
                        "complaints",
                        "complaint_rate",
                    ),
                    "positive_sentiment": _nested_unit(
                        review,
                        "sentiment",
                        "distribution",
                        "positive",
                    ),
                    "average_trust_score": _nested_unit(
                        trust,
                        "trust",
                        "average_trust_score",
                    ),
                },
                components,
            )
        )

    enabled_signals = tuple(
        name
        for name in RECOMMENDATION_WEIGHTS
        if unscored and all(name in components for _, components in unscored)
    )
    available_weight = sum(
        RECOMMENDATION_WEIGHTS[name] for name in enabled_signals
    )
    candidates: list[dict[str, Any]] = []
    if available_weight:
        for candidate, components in unscored:
            weighted_score = sum(
                components[name] * RECOMMENDATION_WEIGHTS[name]
                for name in enabled_signals
            ) / available_weight
            candidate.update(
                {
                    "multi_agent_score": round(weighted_score, 4),
                    "signal_coverage": round(available_weight, 4),
                    "multi_agent_breakdown": {
                        name: round(components[name], 4)
                        for name in enabled_signals
                    },
                }
            )
            candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -float(item["multi_agent_score"]),
            -float(item["signal_coverage"]),
            int(item["source_rank"]),
            _sortable_int(item.get("price")),
            int(item["id"]),
        )
    )
    return {
        "method": "weighted_product_review_trust_v1",
        "weights": dict(RECOMMENDATION_WEIGHTS),
        "missing_signal_policy": "drop_incomplete_signal_globally_then_renormalize",
        "enabled_signals": enabled_signals,
        "omitted_signals": tuple(
            name for name in RECOMMENDATION_WEIGHTS if name not in enabled_signals
        ),
        "count": len(candidates),
        "candidates": candidates,
    }


def _index_analyses(
    analyses: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for analysis in analyses:
        product_id = analysis.get("product_id")
        if isinstance(product_id, int) and not isinstance(product_id, bool):
            indexed[product_id] = analysis
    return indexed


def _unit_interval(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if 0 <= numeric <= 1 else None


def _nested_unit(data: dict[str, Any], *path: str) -> float | None:
    current: object = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _unit_interval(current)


def _sortable_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 2**63 - 1
    return int(value)
