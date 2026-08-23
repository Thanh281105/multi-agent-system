"""Market aggregates and clearly labeled sample knowledge retrieval."""

from __future__ import annotations

import re
from typing import Any

from app.tools.ecommerce import get_product_statistics

SAMPLE_MARKET_DOCUMENTS: tuple[dict[str, str], ...] = (
    {
        "id": "sample_market_audio_2026",
        "title": "Tín hiệu danh mục tai nghe trong bộ dữ liệu demo",
        "content": (
            "Người mua trong dữ liệu mẫu ưu tiên giá, chất lượng âm thanh, pin, "
            "độ thoải mái và tốc độ giao hàng. Đây không phải báo cáo thị trường thật."
        ),
    },
    {
        "id": "sample_market_mobile_2026",
        "title": "Tín hiệu điện thoại trong bộ dữ liệu demo",
        "content": (
            "Các review mẫu thường nhắc màn hình, pin, camera, hiệu năng và giá. "
            "Các tín hiệu chỉ dùng để trình diễn kiến trúc và evaluation."
        ),
    },
    {
        "id": "sample_market_quality_2026",
        "title": "Giới hạn suy luận thị trường",
        "content": (
            "Thống kê từ dữ liệu tổng hợp không đại diện Shopee, Tiki hay Lazada. "
            "Kết luận production phải được tính lại sau khi ingest dữ liệu thật."
        ),
    },
)


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
