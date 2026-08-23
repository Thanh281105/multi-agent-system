"""Default in-process MCP server catalog for sample-backed skills."""

from app.agents.market.skills import analyze_market, search_market_knowledge
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
    """Build a fresh tool router so tests and workers do not share mutable state."""

    router = MCPRouter()
    specs = (
        MCPToolSpec(
            "product_db",
            "search_products",
            "search_products",
            "product.read",
            "Search products from the structured sample database.",
            search_products,
        ),
        MCPToolSpec(
            "product_db",
            "compare_products",
            "compare_products",
            "product.read",
            "Compare structured product facts.",
            compare_products,
        ),
        MCPToolSpec(
            "analytics",
            "rank_products",
            "rank_products",
            "analytics.read",
            "Rank product facts with an explainable formula.",
            rank_products,
        ),
        MCPToolSpec(
            "analytics",
            "get_product_statistics",
            "get_product_statistics",
            "analytics.read",
            "Aggregate structured product facts.",
            get_product_statistics,
        ),
        MCPToolSpec(
            "review_db",
            "get_product_reviews",
            "get_product_reviews",
            "review.read",
            "Retrieve factual reviews for one product.",
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
            "Extract Vietnamese review aspects.",
            extract_review_aspects,
        ),
        MCPToolSpec(
            "analytics",
            "analyze_review_trust",
            "analyze_review_trust",
            "trust.analyze",
            "Estimate explainable review trust signals.",
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
            "Analyze category aggregates over the sample catalog.",
            analyze_market,
        ),
        MCPToolSpec(
            "knowledge",
            "search_market_knowledge",
            "search_market_knowledge",
            "knowledge.read",
            "Retrieve clearly labeled sample market notes.",
            search_market_knowledge,
        ),
    )
    for spec in specs:
        router.register(spec)
    return router
