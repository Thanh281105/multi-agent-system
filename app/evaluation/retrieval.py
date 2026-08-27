"""Protocol-pinned retrieval used only by executable evaluation variants."""

from __future__ import annotations

from typing import Any

from app.knowledge import SAMPLE_MARKET_DOCUMENTS, HashingTextEmbedder

_EMBEDDER = HashingTextEmbedder()
_DOCUMENT_VECTORS = tuple(
    (
        document,
        _EMBEDDER.embed(f"{document['title']} {document['content']}"),
    )
    for document in SAMPLE_MARKET_DOCUMENTS
)


def search_evaluation_sample_knowledge_v2(
    query: str,
    limit: int = 3,
) -> dict[str, Any]:
    """Search pinned sample documents with the declared hashing backend."""

    cleaned = query.strip()
    if not cleaned:
        raise ValueError("query must not be blank")
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    query_vector = _EMBEDDER.embed(cleaned)
    scored = [
        (
            sum(left * right for left, right in zip(query_vector, vector, strict=True)),
            document,
        )
        for document, vector in _DOCUMENT_VECTORS
    ]
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    matches = [
        {
            **document,
            "score": round(score, 6),
            "source": "sample_thesis_dataset",
            "sample_data": True,
        }
        for score, document in scored[:limit]
    ]
    return {
        "count": len(matches),
        "documents": matches,
        "query": cleaned,
        "method": _EMBEDDER.method,
    }
