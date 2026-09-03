"""Cross-sectional market aggregates over the historical book snapshot."""

from __future__ import annotations

from typing import Any

from app.tools.ecommerce import get_product_statistics


def analyze_market(
    category: str | None = None,
    author: str | None = None,
    publisher: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rating: float | None = None,
) -> dict[str, Any]:
    """Expose book-snapshot aggregates with explicit inference boundaries."""

    return {
        "statistics": get_product_statistics(
            category=category,
            author=author,
            publisher=publisher,
            min_price=min_price,
            max_price=max_price,
            min_rating=min_rating,
        ),
        "scope": "historical_tiki_books_snapshot",
        "analysis_type": "cross_sectional_snapshot",
        "representative_of_real_market": False,
        "live_market_data": False,
        "trend_analysis": False,
        "limitations": [
            "Không dùng để suy ra xu hướng, thị phần, nhu cầu hoặc giá hiện tại.",
        ],
        "method": "book_snapshot_database_aggregate_v2",
    }
