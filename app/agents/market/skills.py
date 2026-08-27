"""Market aggregates and clearly labeled sample knowledge retrieval."""

from __future__ import annotations

import re
from typing import Any

from app.knowledge.sample import SAMPLE_MARKET_DOCUMENTS
from app.tools.ecommerce import get_product_statistics


def analyze_market(category: str | None = None) -> dict[str, Any]:
    """Expose database aggregates with an explicit sample-data disclaimer."""

    return {
        "statistics": get_product_statistics(category=category),
        "scope": "synthetic_sample_catalog",
        "representative_of_real_market": False,
        "method": "database_aggregate_v1",
    }


def search_market_knowledge(query: str, limit: int = 3) -> dict[str, Any]:
    """Lexically retrieve sample market notes without pretending they are reports."""

    cleaned = query.strip()
    if not cleaned:
        raise ValueError("query must not be blank")
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    query_tokens = set(re.findall(r"\w+", cleaned.casefold(), flags=re.UNICODE))
    scored: list[tuple[int, dict[str, str]]] = []
    for document in SAMPLE_MARKET_DOCUMENTS:
        haystack = f"{document['title']} {document['content']}".casefold()
        score = sum(token in haystack for token in query_tokens)
        if score:
            scored.append((score, document))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    matches = [
        {
            **document,
            "score": score,
            "source": "sample_thesis_dataset",
            "sample_data": True,
        }
        for score, document in scored[:limit]
    ]
    return {
        "count": len(matches),
        "documents": matches,
        "query": cleaned,
        "method": "lexical_overlap_v1",
    }
