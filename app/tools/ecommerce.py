"""Plain Python database tools exposed to the OpenAI agent."""

import logging
from typing import Any

from app.db.session import session_scope
from app.repositories.ecommerce import (
    EcommerceRepository,
    product_comparison_fact,
    product_provenance,
)

logger = logging.getLogger(__name__)


def search_products(
    query: str | None = None,
    category: str | None = None,
    max_price: int | None = None,
    min_price: int | None = None,
    min_rating: float | None = None,
    author: str | None = None,
    publisher: str | None = None,
    min_page_count: int | None = None,
    max_page_count: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search the cleaned historical Tiki Books snapshot.

    Search by title/free text, author, publisher, category, price, rating, or
    page count. Results are historical snapshot facts, not Tiki's live catalog.
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
    author = _validate_optional_text(author, "author", maximum=220)
    publisher = _validate_optional_text(publisher, "publisher", maximum=220)
    _validate_page_count(min_page_count, "min_page_count")
    _validate_page_count(max_page_count, "max_page_count")
    if (
        min_page_count is not None
        and max_page_count is not None
        and min_page_count > max_page_count
    ):
        raise ValueError("min_page_count cannot exceed max_page_count")

    active_filters = sum(
        value is not None
        for value in (
            query,
            category,
            max_price,
            min_price,
            min_rating,
            author,
            publisher,
            min_page_count,
            max_page_count,
        )
    )
    logger.info(
        "TOOL_CALL tool=search_products active_filters=%d limit=%d",
        active_filters,
        limit,
    )
    with session_scope() as session:
        products = EcommerceRepository(session).search_products(
            query=query,
            category=category,
            max_price=max_price,
            min_price=min_price,
            min_rating=min_rating,
            author=author,
            publisher=publisher,
            min_page_count=min_page_count,
            max_page_count=max_page_count,
            limit=limit,
        )

    result: dict[str, Any] = {"count": len(products), "products": products}
    if products:
        result["provenance"] = _collect_provenance(products)
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
        "TOOL_CALL tool=get_product_reviews limit=%d",
        limit,
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

    product_payload: dict[str, Any] = {
        "id": product.id,
        "name": product.name,
        "authors": list(product.authors),
        "publisher": product.publisher,
        "category": product.category,
        "page_count": product.page_count,
        "rating": product.rating,
        "source_review_count": product.source_review_count,
        "provenance": product_provenance(
            product,
            fields=(
                "rating",
                "title",
                "content",
                "helpful_count",
                "created_at",
                "source_review_count",
            ),
            fallback_source_id="postgresql:reviews",
        ),
    }
    result: dict[str, Any] = {
        "found": True,
        "product": product_payload,
        "count": len(reviews),
        "reviews": [
            {
                "id": review.id,
                "product_id": review.product_id,
                "rating": review.rating,
                "title": review.title,
                "content": review.content,
                "helpful_count": review.helpful_count,
                "created_at": (
                    review.created_at.isoformat()
                    if review.created_at is not None
                    else None
                ),
            }
            for review in reviews
        ],
    }
    result["provenance"] = [product_payload["provenance"]]
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
        "TOOL_CALL tool=compare_products product_count=%d",
        len(product_ids),
    )
    with session_scope() as session:
        products = EcommerceRepository(session).get_products_by_ids(product_ids)

    found_ids = {product.id for product in products}
    comparison_products = [product_comparison_fact(product) for product in products]
    result: dict[str, Any] = {
        "count": len(products),
        "requested_product_ids": product_ids,
        "missing_product_ids": [
            product_id for product_id in product_ids if product_id not in found_ids
        ],
        "products": comparison_products,
    }
    if products:
        result["provenance"] = _collect_provenance(comparison_products)
    logger.info("TOOL RESULT tool=compare_products products=%d", len(products))
    return result


def get_product_statistics(
    category: str | None = None,
    author: str | None = None,
    publisher: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rating: float | None = None,
) -> dict[str, Any]:
    """Return cross-sectional aggregates for the historical books snapshot."""

    if category is not None:
        category = category.strip()
        if not category:
            raise ValueError("category must not be blank")
        if len(category) > 80:
            raise ValueError("category must contain at most 80 characters")
    author = _validate_optional_text(author, "author", maximum=220)
    publisher = _validate_optional_text(publisher, "publisher", maximum=220)
    if min_price is not None and min_price < 0:
        raise ValueError("min_price must be non-negative")
    if max_price is not None and max_price < 0:
        raise ValueError("max_price must be non-negative")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("min_price cannot exceed max_price")
    if min_rating is not None and not 0 <= min_rating <= 5:
        raise ValueError("min_rating must be between 0 and 5")

    logger.info(
        "TOOL_CALL tool=get_product_statistics category_present=%s",
        category is not None,
    )
    with session_scope() as session:
        result = EcommerceRepository(session).get_product_statistics(
            category=category,
            author=author,
            publisher=publisher,
            min_price=min_price,
            max_price=max_price,
            min_rating=min_rating,
        )
    logger.info(
        "TOOL RESULT tool=get_product_statistics products=%d",
        result["product_count"],
    )
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


def _validate_page_count(value: int | None, field_name: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 20_000
    ):
        raise ValueError(f"{field_name} must be an integer between 1 and 20000")


def _validate_optional_text(
    value: str | None,
    field_name: str,
    *,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank")
    if len(cleaned) > maximum:
        raise ValueError(f"{field_name} must contain at most {maximum} characters")
    return cleaned


def _collect_provenance(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect unique provenance records from product fact rows."""

    unique: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for record in records:
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            continue
        source_type = provenance.get("source_type")
        source_id = provenance.get("source_id")
        fields_value = provenance.get("fields", [])
        if not isinstance(source_type, str) or not isinstance(source_id, str):
            continue
        if not isinstance(fields_value, list) or not all(
            isinstance(field, str) for field in fields_value
        ):
            continue
        key = (source_type, source_id, tuple(fields_value))
        unique.setdefault(key, provenance)
    return list(unique.values())
