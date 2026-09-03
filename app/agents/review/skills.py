"""Deterministic Vietnamese review analysis for reproducible evaluation."""

from __future__ import annotations

from collections import Counter
from typing import Any

POSITIVE_TERMS = (
    "tốt",
    "hay",
    "đẹp",
    "nhanh",
    "ổn",
    "hợp lý",
    "hiệu quả",
    "rõ",
    "dễ hiểu",
    "cẩn thận",
    "đáng tiền",
)
NEGATIVE_TERMS = (
    "khó",
    "chậm",
    "móp",
    "cao",
    "nhỏ",
    "lỗi",
    "sai",
    "thiếu",
    "nhầm",
    "trễ",
    "méo",
    "cong",
    "ướt",
    "xước",
    "bong",
    "tróc",
    "gãy",
    "mờ",
    "nhòe",
    "phí",
    "tệ",
    "chưa thật",
    "chưa đủ",
    "chưa cẩn thận",
    "không được cuốn hút",
    "không tốt",
    "không ổn",
)
ASPECT_TERMS: dict[str, tuple[str, ...]] = {
    "nội dung": (
        "nội dung",
        "nội dụng",
        "cốt truyện",
        "kiến thức",
        "bài tập",
        "mẹo",
        "trình bày",
    ),
    "dịch thuật/biên tập": (
        "bản dịch",
        "văn dịch",
        "dịch thuật",
        "biên tập",
        "lỗi chính tả",
    ),
    "giấy/in": (
        "giấy",
        "trang giấy",
        "mực in",
        "in ấn",
        "chữ in",
    ),
    "bìa/đóng gáy": (
        "bìa",
        "gáy",
        "bung gáy",
        "cong góc",
        "móp",
        "gãy",
        "tróc",
        "xước",
    ),
    "đóng gói/giao hàng": (
        "đóng gói",
        "gói hàng",
        "bọc chống sốc",
        "giao hàng",
        "giao chậm",
        "giao trễ",
        "vận chuyển",
        "hộp",
    ),
    "sai/thiếu sách hoặc tập": (
        "giao nhầm",
        "giao sai",
        "giao hàng sai",
        "thiếu sách",
        "thiếu tập",
        "sai tập",
        "nhầm tập",
        "thiếu bookcare",
    ),
    "giá": ("giá", "tầm tiền", "đắt", "rẻ", "đồng tiền"),
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
        for aspect, terms in ASPECT_TERMS.items():
            if any(term in content for term in terms):
                mentions[aspect] += 1
                if _has_local_negative_context(content, terms):
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
            aspect for aspect in ordered if negative_mentions[aspect] > 0
        ],
        "method": "book_keyword_aspects_vi_v2",
    }


def _has_local_negative_context(
    content: str,
    aspect_terms: tuple[str, ...],
) -> bool:
    """Keep aspect polarity local so mixed book feedback is not flattened."""

    for aspect_term in aspect_terms:
        start = content.find(aspect_term)
        while start >= 0:
            window_start = max(0, start - 28)
            window_end = min(len(content), start + len(aspect_term) + 28)
            window = content[window_start:window_end]
            if any(term in window for term in NEGATIVE_TERMS):
                return True
            start = content.find(aspect_term, start + len(aspect_term))
    return False
