"""Transparent product scoring over database facts."""

from __future__ import annotations

from math import log1p
from typing import Any


def rank_products(products: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank book candidates with an explicit, deterministic snapshot formula."""

    if len(products) > 50:
        raise ValueError("rank_products accepts at most 50 products")
    if not products:
        return {
            "count": 0,
            "products": [],
            "method": "weighted_rating_popularity_affordability_v1",
        }

    required_fields = {"id", "name", "price", "rating", "sold_count"}
    for product in products:
        missing = required_fields - product.keys()
        if missing:
            raise ValueError(f"product facts are missing fields: {sorted(missing)}")
        rating = product["rating"]
        sold_count = product["sold_count"]
        if rating is not None and (float(rating) < 0 or float(rating) > 5):
            raise ValueError("product rating must be between 0 and 5")
        if int(product["price"]) < 0 or (
            sold_count is not None and int(sold_count) < 0
        ):
            raise ValueError("product price and sold_count must be non-negative")

    maximum_price = max(int(product["price"]) for product in products) or 1
    maximum_sales = max(int(product["sold_count"] or 0) for product in products)
    sales_denominator = log1p(maximum_sales) or 1.0
    ranked: list[dict[str, Any]] = []
    for product in products:
        rating_score = float(product["rating"] or 0) / 5
        popularity_score = log1p(int(product["sold_count"] or 0)) / sales_denominator
        affordability_score = 1 - (int(product["price"]) / maximum_price)
        total_score = (
            rating_score * 0.45 + popularity_score * 0.35 + affordability_score * 0.20
        )
        ranked.append(
            {
                **product,
                "ranking_score": round(total_score, 4),
                "score_breakdown": {
                    "rating": round(rating_score, 4),
                    "popularity": round(popularity_score, 4),
                    "affordability": round(affordability_score, 4),
                },
                "missing_ranking_facts": [
                    field
                    for field in ("rating", "sold_count")
                    if product[field] is None
                ],
            }
        )
    ranked.sort(
        key=lambda item: (
            -float(item["ranking_score"]),
            int(item["price"]),
            int(item["id"]),
        )
    )
    return {
        "count": len(ranked),
        "products": ranked,
        "method": "weighted_rating_popularity_affordability_v1",
        "weights": {"rating": 0.45, "popularity": 0.35, "affordability": 0.20},
    }
