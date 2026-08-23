"""Deterministic Vietnamese review analysis for reproducible evaluation."""

from __future__ import annotations

from collections import Counter
from typing import Any

POSITIVE_TERMS = (
    "tốt",
    "đẹp",
    "nhanh",
    "ổn",
    "mượt",
    "hợp lý",
    "hiệu quả",
    "chắc chắn",
    "êm",
    "rõ",
)
NEGATIVE_TERMS = (
    "đau",
    "yếu",
    "khó",
    "nóng",
    "chậm",
    "móp",
    "cao",
    "ồn",
    "nhỏ",
    "nồng",
    "lỗi",
    "kích ứng",
    "chưa thật",
    "chưa đủ",
    "không tốt",
    "không ổn",
)
ASPECT_TERMS: dict[str, tuple[str, ...]] = {
    "âm thanh": ("âm thanh", "âm bass", "âm lượng", "chất âm", "micro"),
    "pin": ("pin", "sạc"),
    "kết nối": ("kết nối", "ứng dụng", "phần mềm"),
    "thiết kế": ("thiết kế", "đẹp", "màn hình", "đèn"),
    "thoải mái": ("đeo", "đệm tai", "đau tai", "bám tai"),
    "giao hàng": ("giao hàng", "giao nhanh", "đóng gói", "móp hộp"),
    "giá": ("giá", "tầm tiền"),
    "hiệu năng": ("mượt", "nhanh", "quạt", "tác vụ"),
}


def _validated_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(reviews) > 200:
        raise ValueError("review analysis accepts at most 200 reviews")
    for review in reviews:
        if not {"id", "rating", "content"}.issubset(review):
            raise ValueError("each review needs id, rating, and content")
        rating = int(review["rating"])
        if not 1 <= rating <= 5:
            raise ValueError("review rating must be between 1 and 5")
        if not isinstance(review["content"], str):
            raise ValueError("review content must be a string")
    return reviews


def analyze_review_sentiment(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify sentiment with rating-first, Vietnamese-keyword rules."""

    validated = _validated_reviews(reviews)
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    for review in validated:
        rating = int(review["rating"])
        content = str(review["content"]).casefold()
        positive_hits = sum(term in content for term in POSITIVE_TERMS)
        negative_hits = sum(term in content for term in NEGATIVE_TERMS)
        if rating >= 4 and positive_hits >= negative_hits:
            label = "positive"
        elif rating <= 2 or (rating == 3 and negative_hits > positive_hits):
            label = "negative"
        else:
            label = "neutral"
        counts[label] += 1
        items.append(
            {
                "review_id": int(review["id"]),
                "sentiment": label,
                "positive_term_hits": positive_hits,
                "negative_term_hits": negative_hits,
            }
        )

    total = len(validated)
    distribution = {
        label: round(counts[label] / total, 4) if total else 0.0
        for label in ("positive", "neutral", "negative")
    }
    return {
        "count": total,
        "distribution": distribution,
        "items": items,
        "method": "rating_keyword_rules_vi_v1",
    }


def extract_review_aspects(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Count mentioned aspects and mark negative mentions transparently."""

    validated = _validated_reviews(reviews)
    mentions: Counter[str] = Counter()
    negative_mentions: Counter[str] = Counter()
    for review in validated:
        content = str(review["content"]).casefold()
        negative_context = int(review["rating"]) <= 3 or any(
            term in content for term in NEGATIVE_TERMS
        )
        for aspect, terms in ASPECT_TERMS.items():
            if any(term in content for term in terms):
                mentions[aspect] += 1
                if negative_context:
                    negative_mentions[aspect] += 1

    ordered = sorted(mentions, key=lambda item: (-mentions[item], item))
    return {
        "count": len(validated),
        "aspects": [
            {
                "name": aspect,
                "mentions": mentions[aspect],
                "negative_mentions": negative_mentions[aspect],
            }
            for aspect in ordered
        ],
        "negative_aspects": [
            aspect
            for aspect in ordered
            if negative_mentions[aspect] > 0
        ],
        "method": "keyword_aspects_vi_v1",
    }
