"""Explainable Vietnamese intent routing and entity extraction."""

from __future__ import annotations

import re
from typing import Any

from app.orchestrator.schemas import RoutedIntent
from app.shared.session import SessionState

CATEGORY_ALIASES: dict[str, str] = {
    "tai nghe": "Tai nghe",
    "điện thoại": "Điện thoại",
    "smartphone": "Điện thoại",
    "laptop": "Laptop",
    "máy tính xách tay": "Laptop",
    "mỹ phẩm": "Mỹ phẩm",
    "đồ gia dụng": "Đồ gia dụng",
    "gia dụng": "Đồ gia dụng",
    "phụ kiện": "Phụ kiện",
}
FOLLOW_UP_PREFIXES = ("còn ", "thế ", "vậy ", "nó ", "sản phẩm đó")


class IntentRouter:
    """Route common thesis-demo use cases without requiring an LLM call."""

    def route(self, message: str, session: SessionState) -> RoutedIntent:
        cleaned = " ".join(message.strip().split())
        normalized = cleaned.casefold()
        entities = self._extract_entities(cleaned)

        if self._is_follow_up(normalized) and session.active_agent:
            product_id = session.state.get("last_product_id")
            if isinstance(product_id, int):
                entities["product_id"] = product_id
            follow_up_intent = {
                "product_agent": "product.follow_up",
                "review_agent": "review.summary",
                "trust_agent": "trust.complaints",
                "market_agent": "market.search",
            }.get(session.active_agent, "general.help")
            return RoutedIntent(
                intent=follow_up_intent,
                confidence=0.86,
                entities=entities,
                routing_rule="session_agent_pinning",
            )

        has_complaint = any(
            term in normalized
            for term in ("phàn nàn", "complaint", "khách chê", "vấn đề gì")
        )
        has_product_discovery = any(
            term in normalized
            for term in ("tìm ", "gợi ý", "nên mua", "bán tốt", "sản phẩm")
        )
        if has_complaint and has_product_discovery:
            return self._route(
                "multi.recommendation",
                0.96,
                entities,
                "product_and_complaint_keywords",
            )
        if "so sánh" in normalized or "compare" in normalized:
            entities["product_queries"] = self._extract_comparison_queries(cleaned)
            return self._route(
                "product.compare",
                0.97,
                entities,
                "comparison_keyword",
            )
        if any(term in normalized for term in ("thị trường", "xu hướng", "phân khúc")):
            return self._route(
                "market.analyze",
                0.94,
                entities,
                "market_keyword",
            )
        if has_complaint:
            return self._route(
                "trust.complaints",
                0.94,
                entities,
                "complaint_keyword",
            )
        if any(
            term in normalized
            for term in ("review", "đánh giá", "nhận xét", "khách hàng nói")
        ):
            return self._route(
                "review.summary",
                0.92,
                entities,
                "review_keyword",
            )
        if any(
            term in normalized
            for term in ("bán tốt", "tốt nhất", "xếp hạng", "gợi ý", "nên mua")
        ):
            return self._route(
                "product.rank",
                0.90,
                entities,
                "ranking_keyword",
            )
        if any(alias in normalized for alias in CATEGORY_ALIASES) or any(
            term in normalized for term in ("tìm", "giá", "rating", "sao")
        ):
            return self._route(
                "product.search",
                0.82,
                entities,
                "product_filter_keyword",
            )
        if any(term in normalized for term in ("xin chào", "hello", "giúp gì")):
            return self._route("general.help", 0.99, {}, "greeting")
        return self._route("general.unsupported", 0.55, {}, "no_supported_domain")

    @staticmethod
    def _route(
        intent: str,
        confidence: float,
        entities: dict[str, Any],
        rule: str,
    ) -> RoutedIntent:
        return RoutedIntent(
            intent=intent,
            confidence=confidence,
            entities=entities,
            routing_rule=rule,
        )

    def _extract_entities(self, message: str) -> dict[str, Any]:
        normalized = message.casefold()
        entities: dict[str, Any] = {"query": message}
        for alias, category in CATEGORY_ALIASES.items():
            if alias in normalized:
                entities["category"] = category
                break

        max_price = self._extract_money(
            normalized,
            prefixes=("dưới", "tối đa", "không quá", "nhỏ hơn"),
        )
        min_price = self._extract_money(
            normalized,
            prefixes=("trên", "tối thiểu", "từ"),
        )
        if max_price is not None:
            entities["max_price"] = max_price
        if min_price is not None:
            entities["min_price"] = min_price

        rating_match = re.search(
            r"(?:rating|đánh giá)\s*(?:ít nhất|từ)?\s*(\d(?:[.,]\d+)?)"
            r"|(?:ít nhất|từ)\s*(\d(?:[.,]\d+)?)\s*sao",
            normalized,
        )
        if rating_match:
            rating_value = rating_match.group(1) or rating_match.group(2)
            entities["min_rating"] = float(rating_value.replace(",", "."))

        id_match = re.search(r"(?:product[_ ]?id|mã)\s*[:#]?\s*(\d+)", normalized)
        if id_match:
            entities["product_id"] = int(id_match.group(1))

        product_query = self._extract_subject_after_marker(message)
        if product_query is None:
            product_query = self._extract_search_subject(message)
        if product_query:
            entities["product_query"] = product_query
        return entities

    @staticmethod
    def _extract_money(text: str, *, prefixes: tuple[str, ...]) -> int | None:
        prefix_pattern = "|".join(re.escape(prefix) for prefix in prefixes)
        match = re.search(
            rf"(?:{prefix_pattern})\s*(\d(?:[\d.,]*\d)?)\s*"
            r"(triệu|tr|nghìn|ngàn|k)?",
            text,
        )
        if not match:
            return None
        raw_value = match.group(1)
        unit = match.group(2)
        try:
            if unit:
                value = float(raw_value.replace(",", "."))
            elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw_value):
                value = float(raw_value.replace(".", "").replace(",", ""))
            else:
                value = float(raw_value.replace(",", "."))
        except ValueError:
            return None
        if unit in {"triệu", "tr"}:
            value *= 1_000_000
        elif unit in {"nghìn", "ngàn", "k"}:
            value *= 1_000
        return int(value)

    @staticmethod
    def _extract_subject_after_marker(message: str) -> str | None:
        match = re.search(
            r"(?:về|của|cho)\s+(.+?)(?:[?.!]|$)",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        subject = match.group(1).strip(" ,.;:?!")
        return subject[:220] if subject else None

    @staticmethod
    def _extract_search_subject(message: str) -> str | None:
        match = re.search(
            r"^\s*tìm(?:\s+(?:cho tôi|giúp tôi|giúp))?\s+(.+?)"
            r"(?=\s+(?:dưới|trên|tối đa|tối thiểu|rating|giá)\b|[,.?!]|$)",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        subject = match.group(1).strip(" ,.;:?!")
        return subject[:220] if subject else None

    @staticmethod
    def _extract_comparison_queries(message: str) -> list[str]:
        tail = re.split(r"so sánh", message, maxsplit=1, flags=re.IGNORECASE)[-1]
        tail = re.split(
            r"(?:nếu|theo|ưu tiên|thì)",
            tail,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        parts = re.split(r"\s+(?:với|và)\s+|,", tail, maxsplit=2)
        return [part.strip(" .?!")[:220] for part in parts if part.strip(" .?!")][:5]

    @staticmethod
    def _is_follow_up(normalized: str) -> bool:
        return normalized.startswith(FOLLOW_UP_PREFIXES) or normalized.endswith(
            "thì sao?"
        )
