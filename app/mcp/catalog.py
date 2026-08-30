"""Default in-process MCP server catalog for sample-backed skills."""

from app.agents.market.skills import analyze_market
from app.agents.product.skills import rank_products
from app.agents.review.skills import analyze_review_sentiment, extract_review_aspects
from app.agents.trust.skills import analyze_review_trust, detect_complaints
from app.mcp.router import MCPRouter, MCPToolSpec
from app.tools.ecommerce import (
    compare_products,
    get_product_reviews,
    get_product_statistics,
    search_products,
)


def build_default_mcp_router() -> MCPRouter:
    """Build the historical Tiki Books tool router with isolated mutable state."""

    router = MCPRouter()
    specs = (
        MCPToolSpec(
            "product_db",
            "search_products",
            "search_products",
            "product.read",
            "Search cleaned historical Tiki book facts.",
            search_products,
        ),
        MCPToolSpec(
            "product_db",
            "compare_products",
            "compare_products",
            "product.read",
            "Compare structured historical book facts.",
            compare_products,
        ),
        MCPToolSpec(
            "analytics",
            "rank_products",
            "rank_products",
            "analytics.read",
            "Rank book candidates with an explainable snapshot formula.",
            rank_products,
        ),
        MCPToolSpec(
            "analytics",
            "get_product_statistics",
            "get_product_statistics",
            "analytics.read",
            "Aggregate cross-sectional Tiki Books snapshot facts.",
            get_product_statistics,
        ),
        MCPToolSpec(
            "review_db",
            "get_product_reviews",
            "get_product_reviews",
            "review.read",
            "Retrieve cleaned sampled reviews for one book.",
            get_product_reviews,
        ),
        MCPToolSpec(
            "analytics",
            "analyze_review_sentiment",
            "analyze_review_sentiment",
            "analytics.read",
            "Analyze Vietnamese review sentiment deterministically.",
            analyze_review_sentiment,
        ),
        MCPToolSpec(
            "analytics",
            "extract_review_aspects",
            "extract_review_aspects",
            "analytics.read",
            "Extract book-specific Vietnamese review aspects.",
            extract_review_aspects,
        ),
        MCPToolSpec(
            "analytics",
            "analyze_review_trust",
            "analyze_review_trust",
            "trust.analyze",
            "Describe explainable review-text-quality signals.",
            analyze_review_trust,
        ),
        MCPToolSpec(
            "analytics",
            "detect_complaints",
            "detect_complaints",
            "trust.analyze",
            "Detect complaint signals in Vietnamese reviews.",
            detect_complaints,
        ),
        MCPToolSpec(
            "analytics",
            "analyze_market",
            "analyze_market",
            "analytics.read",
            "Analyze cross-sectional aggregates over the historical book snapshot.",
            analyze_market,
        ),
    )
    for spec in specs:
        router.register(spec)
    return router
