"""Deterministic, privacy-preserving cleaning for the Tiki Books archive."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from app.data.contracts import NormalizedProduct, NormalizedReview
from app.data.profiles import ProfileConfig

CLEANER_VERSION = "tiki-books-cleaner-1.0.0"
TAXONOMY_VERSION = "tiki-books-taxonomy-1.0.0"
COVER_HOST_ALLOWLIST = frozenset({"salt.tikicdn.com"})

_CATEGORY_ALIASES = {
    "kiến thức bách khoa": "Kiến thức - Bách khoa",
    "lĩnh vực khác": "Khác",
    "tiểu thuyết": "Tiểu thuyết",
}
_AUTHOR_SEPARATOR_RE = re.compile(r"\s*(?:,|;|\|)\s*")
_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+")
_EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w.-])"
)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)(?:[ .-]?\d){8,10}(?!\d)")


@dataclass(slots=True)
class CleaningStats:
    """Only aggregate counters; never raw rows or review text."""

    input_counts: Counter[str] = field(default_factory=Counter)
    correction_counts: Counter[str] = field(default_factory=Counter)
    drop_counts: Counter[str] = field(default_factory=Counter)
    redaction_counts: Counter[str] = field(default_factory=Counter)


class RowRejected(ValueError):
    """A row failed a named policy without retaining its source values."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def clean_products(
    rows: Iterable[Mapping[str, str]],
    stats: CleaningStats,
) -> list[NormalizedProduct]:
    """Normalize and deterministically deduplicate product rows."""

    candidates: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        stats.input_counts["book_rows"] += 1
        external_id = clean_text(row.get("product_id", ""))
        if not external_id or len(external_id) > 128:
            stats.drop_counts["product_missing_or_invalid_id"] += 1
            continue
        candidates[external_id].append(row)

    products: list[NormalizedProduct] = []
    for external_id in sorted(candidates):
        rows_for_id = candidates[external_id]
        if len(rows_for_id) > 1:
            stats.correction_counts["product_duplicate_identity"] += (
                len(rows_for_id) - 1
            )
            if len({_row_hash(row) for row in rows_for_id}) > 1:
                stats.correction_counts["product_duplicate_conflict"] += 1
        chosen = max(rows_for_id, key=_product_dedupe_key)
        try:
            products.append(_normalize_product(chosen, stats))
        except RowRejected as exc:
            stats.drop_counts[exc.reason] += 1

    products.sort(key=lambda item: item.external_id)
    return products


def clean_reviews(
    rows: Iterable[Mapping[str, str]],
    *,
    product_external_ids: set[str],
    stats: CleaningStats,
) -> list[NormalizedReview]:
    """Normalize reviews, redact PII, and dedupe only source identity."""

    candidates: dict[str, list[NormalizedReview]] = defaultdict(list)
    for row in rows:
        stats.input_counts["review_rows"] += 1
        product_id = clean_text(row.get("product_id", ""))
        comment_id = clean_text(row.get("comment_id", ""))
        if not product_id or not comment_id:
            stats.drop_counts["review_missing_identity"] += 1
            continue
        if len(product_id) > 128 or len(comment_id) > 128:
            stats.drop_counts["review_invalid_identity"] += 1
            continue
        if product_id not in product_external_ids:
            stats.drop_counts["review_orphan"] += 1
            continue
        try:
            review = _normalize_review(row, product_id, comment_id, stats)
        except RowRejected as exc:
            stats.drop_counts[exc.reason] += 1
            continue
        candidates[review.external_id].append(review)

    reviews: list[NormalizedReview] = []
    for external_id in sorted(candidates):
        rows_for_id = candidates[external_id]
        if len(rows_for_id) > 1:
            stats.drop_counts["review_duplicate_identity"] += len(rows_for_id) - 1
            serialized = {_model_hash(review) for review in rows_for_id}
            if len(serialized) > 1:
                stats.correction_counts["review_duplicate_conflict"] += 1
        reviews.append(max(rows_for_id, key=_review_dedupe_key))
    return reviews


def select_profile_products(
    products: list[NormalizedProduct],
    reviews: list[NormalizedReview],
    *,
    config: ProfileConfig,
    seed: int,
) -> list[NormalizedProduct]:
    """Sample only after the full valid source has been cleaned."""

    if config.target_products is None:
        return sorted(products, key=lambda item: item.external_id)

    review_product_ids = {review.product_external_id for review in reviews}
    eligible = {
        product.external_id: product
        for product in products
        if product.external_id in review_product_ids
    }
    selected: list[NormalizedProduct] = []

    buckets: dict[str, list[NormalizedProduct]] = defaultdict(list)
    for product in eligible.values():
        buckets[product.category].append(product)
    for values in buckets.values():
        values.sort(key=lambda item: _stable_rank(seed, "product", item.external_id))

    categories = sorted(buckets)
    while len(selected) < config.target_products:
        progress = False
        for category in categories:
            values = buckets[category]
            if values:
                selected.append(values.pop(0))
                progress = True
                if len(selected) == config.target_products:
                    break
        if not progress:
            break
    return sorted(selected, key=lambda item: item.external_id)


def select_profile_reviews(
    reviews: list[NormalizedReview],
    *,
    selected_product_ids: set[str],
    config: ProfileConfig,
    seed: int,
) -> list[NormalizedReview]:
    """Keep all full-profile reviews or rating-stratify bounded profiles."""

    eligible = [
        review
        for review in reviews
        if review.product_external_id in selected_product_ids
    ]
    if config.max_reviews_per_product is None:
        return sorted(eligible, key=lambda item: item.external_id)

    grouped: dict[str, dict[int, list[NormalizedReview]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for review in eligible:
        grouped[review.product_external_id][review.rating].append(review)

    selected: list[NormalizedReview] = []
    for product_id in sorted(grouped):
        rating_groups = grouped[product_id]
        for values in rating_groups.values():
            values.sort(key=lambda item: _stable_rank(seed, "review", item.external_id))
        positions = {rating: 0 for rating in range(1, 6)}
        selected_for_product = 0
        while selected_for_product < config.max_reviews_per_product:
            added = False
            for rating in range(1, 6):
                values = rating_groups.get(rating, [])
                position = positions[rating]
                if position < len(values):
                    selected.append(values[position])
                    positions[rating] = position + 1
                    selected_for_product += 1
                    added = True
                    if selected_for_product == config.max_reviews_per_product:
                        break
            if not added:
                break
    if config.target_reviews is not None and len(selected) > config.target_reviews:
        selected = sorted(
            selected,
            key=lambda item: _stable_rank(
                seed,
                "profile-review",
                item.external_id,
            ),
        )[: config.target_reviews]
    return sorted(selected, key=lambda item: item.external_id)


def clean_text(value: object) -> str:
    """Decode entities, remove controls, normalize NFC, and collapse whitespace."""

    decoded = html.unescape(str(value or ""))
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in decoded
    )
    return _WHITESPACE_RE.sub(
        " ", unicodedata.normalize("NFC", without_controls)
    ).strip()


def contains_pii(value: str) -> bool:
    """Return whether a normalized review still matches a defined PII pattern."""

    return any(pattern.search(value) for pattern in (_URL_RE, _EMAIL_RE, _PHONE_RE))


def _normalize_product(
    row: Mapping[str, str],
    stats: CleaningStats,
) -> NormalizedProduct:
    external_id = clean_text(row.get("product_id", ""))
    title = _bounded_text(row.get("title", ""), 220, "product_title", stats)
    if not title:
        raise RowRejected("product_missing_title")

    price = _parse_optional_int(row.get("current_price", ""))
    if price is None or not 0 <= price <= 1_000_000_000:
        raise RowRejected("product_invalid_price")
    original_price = _parse_optional_int(row.get("original_price", ""))
    if original_price is not None and not 0 <= original_price <= 1_000_000_000:
        original_price = None
        stats.correction_counts["product_invalid_original_price"] += 1
    if original_price is not None and original_price < price:
        original_price = None
        stats.correction_counts["product_original_price_below_price"] += 1

    rating = _parse_optional_float(row.get("avg_rating", ""))
    if rating is not None and not 0 <= rating <= 5:
        rating = None
        stats.correction_counts["product_invalid_rating"] += 1
    page_count = _parse_optional_int(row.get("pages", ""))
    if page_count is not None and not 1 <= page_count <= 20_000:
        page_count = None
        stats.correction_counts["product_invalid_page_count"] += 1
    popularity = _parse_optional_int(row.get("quantity", ""))
    if popularity is not None and popularity < 0:
        popularity = None
        stats.correction_counts["product_invalid_popularity"] += 1
    source_review_count = _parse_optional_int(row.get("n_review", ""))
    if source_review_count is None:
        source_review_count = 0
    elif source_review_count < 0:
        source_review_count = 0
        stats.correction_counts["product_invalid_review_count"] += 1

    authors = _normalize_authors(row.get("authors", ""), stats)
    publisher = (
        _bounded_text(row.get("manufacturer", ""), 220, "product_publisher", stats)
        or None
    )
    source_category = clean_text(row.get("category", ""))
    category = _normalize_category(source_category, title, stats)
    cover_url = _normalize_cover_url(row.get("cover_link", ""), stats)

    description_parts = [title]
    if authors:
        description_parts.append(f"Tác giả: {', '.join(authors)}")
    if publisher:
        description_parts.append(f"Nhà xuất bản: {publisher}")
    description_parts.append(f"Danh mục: {category}")
    if page_count is not None:
        description_parts.append(f"{page_count} trang")
    description = ". ".join(description_parts)
    if len(description) > 4_000:
        description = description[:4_000].rstrip()
        stats.correction_counts["product_description_truncated"] += 1

    return NormalizedProduct(
        external_id=external_id,
        name=title,
        authors=authors,
        publisher=publisher,
        category=category,
        page_count=page_count,
        price_vnd=price,
        original_price_vnd=original_price,
        rating=rating,
        source_popularity=popularity,
        source_review_count=source_review_count,
        cover_url=cover_url,
        description=description,
        seller_name=None,
        source_metadata={
            "source_category": source_category or None,
            "category_was_mapped": category != source_category,
        },
    )


def _normalize_review(
    row: Mapping[str, str],
    product_id: str,
    comment_id: str,
    stats: CleaningStats,
) -> NormalizedReview:
    rating = _parse_optional_int(row.get("rating", ""))
    if rating is None or not 1 <= rating <= 5:
        raise RowRejected("review_invalid_rating")
    content = clean_text(row.get("content", ""))
    if not content:
        raise RowRejected("review_empty_content")
    content = _redact_pii(content, stats)
    if not content:
        raise RowRejected("review_empty_after_cleaning")
    title = clean_text(row.get("title", "")) or None
    if title is not None:
        title = _redact_pii(title, stats)
        if len(title) > 1_000:
            title = title[:1_000].rstrip()
            stats.correction_counts["review_title_truncated"] += 1
    if len(content) > 20_000:
        content = content[:20_000].rstrip()
        stats.correction_counts["review_content_truncated"] += 1
    helpful_count = _parse_optional_int(row.get("thank_count", ""))
    if helpful_count is None:
        helpful_count = 0
    elif helpful_count < 0:
        helpful_count = 0
        stats.correction_counts["review_invalid_helpful_count"] += 1
    return NormalizedReview(
        external_id=f"{product_id}:{comment_id}",
        product_external_id=product_id,
        rating=rating,
        title=title,
        content=content,
        helpful_count=helpful_count,
        created_at=None,
    )


def _normalize_authors(value: object, stats: CleaningStats) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    authors: list[str] = []
    seen: set[str] = set()
    for author in _AUTHOR_SEPARATOR_RE.split(text):
        normalized = clean_text(author)
        if not normalized:
            continue
        if len(normalized) > 220:
            normalized = normalized[:220].rstrip()
            stats.correction_counts["product_author_truncated"] += 1
        key = normalized.casefold()
        if key not in seen:
            authors.append(normalized)
            seen.add(key)
    return authors


def _normalize_category(
    source_category: str,
    title: str,
    stats: CleaningStats,
) -> str:
    if not source_category or source_category.casefold() == title.casefold():
        stats.correction_counts["product_category_to_other"] += 1
        return "Khác"
    category = _CATEGORY_ALIASES.get(source_category.casefold(), source_category)
    if len(category) > 120:
        category = category[:120].rstrip()
        stats.correction_counts["product_category_truncated"] += 1
    if category != source_category:
        stats.correction_counts["product_category_mapped"] += 1
    return category


def _normalize_cover_url(value: object, stats: CleaningStats) -> str | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        stats.correction_counts["product_cover_url_rejected"] += 1
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in COVER_HOST_ALLOWLIST
        or parsed.username is not None
        or parsed.password is not None
    ):
        stats.correction_counts["product_cover_url_rejected"] += 1
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def _redact_pii(value: str, stats: CleaningStats) -> str:
    redacted = value
    for name, pattern, placeholder in (
        ("url", _URL_RE, "[URL]"),
        ("email", _EMAIL_RE, "[EMAIL]"),
        ("phone", _PHONE_RE, "[PHONE]"),
    ):
        redacted, count = pattern.subn(placeholder, redacted)
        stats.redaction_counts[name] += count
    return clean_text(redacted)


def _bounded_text(
    value: object,
    maximum: int,
    field_name: str,
    stats: CleaningStats,
) -> str:
    cleaned = clean_text(value)
    if len(cleaned) <= maximum:
        return cleaned
    stats.correction_counts[f"{field_name}_truncated"] += 1
    return cleaned[:maximum].rstrip()


def _parse_optional_int(value: object) -> int | None:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        try:
            return int(float(cleaned))
        except (OverflowError, ValueError):
            return None


def _parse_optional_float(value: object) -> float | None:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except (OverflowError, ValueError):
        return None


def _product_dedupe_key(row: Mapping[str, str]) -> tuple[int, str]:
    fields = (
        "title",
        "authors",
        "original_price",
        "current_price",
        "quantity",
        "category",
        "n_review",
        "avg_rating",
        "pages",
        "manufacturer",
        "cover_link",
    )
    completeness = sum(bool(clean_text(row.get(field, ""))) for field in fields)
    return completeness, _row_hash(row)


def _review_dedupe_key(review: NormalizedReview) -> tuple[int, str]:
    completeness = (
        1
        + int(review.title is not None)
        + int(review.helpful_count > 0)
        + int(review.created_at is not None)
    )
    return completeness, _model_hash(review)


def _row_hash(row: Mapping[str, str]) -> str:
    payload = {
        clean_text(key): clean_text(value)
        for key, value in sorted(row.items(), key=lambda item: str(item[0]))
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _model_hash(model: NormalizedReview) -> str:
    payload = model.model_dump_json(exclude_none=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_rank(seed: int, kind: str, value: str) -> str:
    return hashlib.sha256(f"{seed}:{kind}:{value}".encode("utf-8")).hexdigest()
