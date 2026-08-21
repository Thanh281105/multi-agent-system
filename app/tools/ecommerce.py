"""Plain Python database tools exposed to the OpenAI agent."""

import logging
from typing import Any

from app.db.session import session_scope
from app.repositories.ecommerce import EcommerceRepository, product_comparison_fact

logger = logging.getLogger(__name__)


def search_products(
    query: str | None = None,
    category: str | None = None,
    max_price: int | None = None,
    min_price: int | None = None,
    min_rating: float | None = None,
    platform: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search Vietnamese e-commerce products using structured filters.

    Use this tool for product discovery, price/rating constraints, category
    searches, and resolving a product name to its database ID. Results contain
    database facts only and are safe to pass back to an LLM for explanation.
    """

    _validate_limit(limit)
    if max_price is not None and max_price < 0:
        raise ValueError("max_price must be non-negative")
    if min_price is not None and min_price < 0:
        raise ValueError("min_price must be non-negative")
    if min_rating is not None and not 0 <= min_rating <= 5:
        raise ValueError("min_rating must be between 0 and 5")
    if max_price is not None and min_price is not None and min_price > max_price:
        raise ValueError("min_price cannot exceed max_price")

    arguments = {
        "query": query,
        "category": category,
        "max_price": max_price,
        "min_price": min_price,
        "min_rating": min_rating,
        "platform": platform,
        "limit": limit,
    }
    logger.info("TOOL CALL tool=search_products arguments=%s", arguments)
    with session_scope() as session:
        products = EcommerceRepository(session).search_products(
            query=query,
            category=category,
            max_price=max_price,
            min_price=min_price,
            min_rating=min_rating,
            platform=platform,
            limit=limit,
        )

    result = {"count": len(products), "products": products}
    logger.info("TOOL RESULT tool=search_products products=%d", len(products))
    return result


def get_product_reviews(product_id: int, limit: int = 20) -> dict[str, Any]:
    """Get factual customer reviews for one product ID.

    If the product does not exist, the response explicitly reports that no
    product was found. Do not infer or invent reviews for a missing product.
    """

    _validate_positive_id(product_id, "product_id")
    _validate_limit(limit, maximum=50)
    logger.info(
        "TOOL CALL tool=get_product_reviews arguments=%s",
        {"product_id": product_id, "limit": limit},
    )
    with session_scope() as session:
        product, reviews = EcommerceRepository(session).get_product_reviews(
            product_id=product_id,
            limit=limit,
        )

    if product is None:
        logger.info("TOOL RESULT tool=get_product_reviews products=0 reviews=0")
        return {
            "found": False,
            "product": None,
            "count": 0,
            "reviews": [],
        }

    result = {
        "found": True,
        "product": {
            "id": product.id,
            "name": product.name,
            "rating": product.rating,
        },
        "count": len(reviews),
        "reviews": [
            {
                "id": review.id,
                "product_id": review.product_id,
                "rating": review.rating,
                "content": review.content,
            }
            for review in reviews
        ],
    }
    logger.info(
        "TOOL RESULT tool=get_product_reviews products=1 reviews=%d",
        len(reviews),
    )
    return result


def compare_products(product_ids: list[int]) -> dict[str, Any]:
    """Return comparable database facts for up to five product IDs.

    This tool does not choose a winner. The agent must explain any
    recommendation from the returned price, rating, popularity, and
    description facts.
    """

    if not product_ids:
        raise ValueError("product_ids must contain at least one ID")
    if len(product_ids) > 5:
        raise ValueError("compare_products accepts at most five product IDs")
    if len(set(product_ids)) != len(product_ids):
        raise ValueError("product_ids must not contain duplicates")
    for product_id in product_ids:
        _validate_positive_id(product_id, "product_id")

    logger.info(
        "TOOL CALL tool=compare_products arguments=%s",
        {"product_ids": product_ids},
    )
    with session_scope() as session:
        products = EcommerceRepository(session).get_products_by_ids(product_ids)

    found_ids = {product.id for product in products}
    result = {
        "count": len(products),
        "requested_product_ids": product_ids,
        "missing_product_ids": [
            product_id for product_id in product_ids if product_id not in found_ids
        ],
        "products": [product_comparison_fact(product) for product in products],
    }
    logger.info("TOOL RESULT tool=compare_products products=%d", len(products))
    return result


def _validate_limit(limit: int, *, maximum: int = 50) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= maximum
    ):
        raise ValueError(f"limit must be an integer between 1 and {maximum}")


def _validate_positive_id(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
