"""Explainable review-quality and complaint heuristics for sample data."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from app.agents.review.skills import ASPECT_TERMS, NEGATIVE_TERMS


def _normalize(content: str) -> str:
    return " ".join(re.findall(r"\w+", content.casefold(), flags=re.UNICODE))


def _validated_rating(review: dict[str, Any]) -> int:
    rating = review["rating"]
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError("review rating must be an integer between 1 and 5")
    return rating


def analyze_review_trust(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate trust using duplicate/generic/length signals, never identity data."""

    if len(reviews) > 200:
        raise ValueError("trust analysis accepts at most 200 reviews")
    normalized = [_normalize(str(review.get("content", ""))) for review in reviews]
    frequencies = Counter(normalized)
    items: list[dict[str, Any]] = []
    for review, content in zip(reviews, normalized, strict=True):
        if "id" not in review or "rating" not in review or not content:
            raise ValueError("each review needs id, rating, and non-empty content")
        _validated_rating(review)
        token_count = len(content.split())
        duplicate = frequencies[content] > 1
        generic = content in {
            "sản phẩm tốt",
            "hàng tốt",
            "ok",
            "good",
        }
        spam_probability = 0.05
        reasons: list[str] = []
        if duplicate:
            spam_probability += 0.65
            reasons.append("duplicate_text")
        if token_count < 4:
            spam_probability += 0.20
            reasons.append("very_short")
        if generic:
            spam_probability += 0.20
            reasons.append("generic_only")
        spam_probability = min(spam_probability, 0.99)
        items.append(
            {
                "review_id": int(review["id"]),
                "spam_probability": round(spam_probability, 4),
                "trust_score": round(1 - spam_probability, 4),
                "signals": reasons,
            }
        )

    average_trust = (
        sum(float(item["trust_score"]) for item in items) / len(items) if items else 0.0
    )
    return {
        "count": len(items),
        "average_trust_score": round(average_trust, 4),
        "suspected_spam_count": sum(
            float(item["spam_probability"]) >= 0.5 for item in items
        ),
        "items": items,
        "method": "duplicate_length_generic_rules_v1",
    }


def detect_complaints(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect complaints from low ratings and explicit negative Vietnamese terms."""

    if len(reviews) > 200:
        raise ValueError("complaint analysis accepts at most 200 reviews")
    complaint_items: list[dict[str, Any]] = []
    aspect_counts: Counter[str] = Counter()
    for review in reviews:
        if not {"id", "rating", "content"}.issubset(review):
            raise ValueError("each review needs id, rating, and content")
        rating = _validated_rating(review)
        content = str(review["content"]).casefold()
        matched_terms = sorted(term for term in NEGATIVE_TERMS if term in content)
        is_complaint = rating <= 3 or bool(matched_terms)
        if not is_complaint:
            continue
        aspects = sorted(
            aspect
            for aspect, terms in ASPECT_TERMS.items()
            if any(term in content for term in terms)
        )
        aspect_counts.update(aspects)
        complaint_items.append(
            {
                "review_id": int(review["id"]),
                "rating": rating,
                "aspects": aspects,
                "signals": matched_terms,
            }
        )

    total = len(reviews)
    return {
        "count": total,
        "complaint_count": len(complaint_items),
        "complaint_rate": round(len(complaint_items) / total, 4) if total else 0.0,
        "top_complaint_aspects": [
            {"name": name, "count": count}
            for name, count in aspect_counts.most_common()
        ],
        "items": complaint_items,
        "method": "rating_negative_keyword_rules_vi_v1",
    }
