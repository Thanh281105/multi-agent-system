"""Explainable Vietnamese intent routing and entity extraction."""

from __future__ import annotations

import json
import re
from typing import Any

from app.orchestrator.model_schemas import RoutingDecision
from app.orchestrator.schemas import RoutedIntent
from app.registry import AgentRegistry, default_registry
from app.shared import (
    ModelRuntime,
    ModelRuntimeError,
    ModelRuntimeMode,
    ReasoningEffort,
    mark_model_call_fallback,
)
from app.shared.session import SessionState

CATEGORY_ALIASES: dict[str, str] = {
    "bài học kinh doanh": "Bài học kinh doanh",
    "chiêm tinh": "Sách Chiêm Tinh - Horoscope",
    "du ký": "Du ký",
    "kiến trúc": "Mỹ Thuật - Kiến Trúc",
    "lập trình": "Lập Trình",
    "light novel": "Light novel",
    "lịch sử thế giới": "Lịch Sử Thế Giới",
    "lịch sử việt nam": "Lịch Sử Việt Nam",
    "luật": "Luật - Văn Bản Luật",
    "lý luận chính trị": "Lý Luận Chính Trị",
}
BOOK_MARKERS = (
    "sách",
    "cuốn",
    "quyển",
    "tác giả",
    "nhà xuất bản",
    "nxb",
)
OUT_OF_DOMAIN_PRODUCT_TERMS = (
    "tai nghe",
    "điện thoại",
    "iphone",
    "smartphone",
    "laptop",
    "máy tính xách tay",
    "mỹ phẩm",
    "đồ gia dụng",
    "tivi",
    "máy giặt",
    "tủ lạnh",
    "đồng hồ",
    "camera",
    "loa bluetooth",
    "bàn phím",
    "chuột máy tính",
    "máy ảnh",
    "quần áo",
    "giày",
)
FOLLOW_UP_PREFIXES = ("còn ", "thế ", "vậy ", "nó ", "cuốn đó", "sách đó")
QUESTION_SUFFIXES = (
    "ra sao",
    "thế nào",
    "như thế nào",
    "như nào",
    "thế nào rồi",
    "sao",
    "ổn không",
    "tốt không",
    "có tốt không",
    "có nên mua không",
    "được không",
    "không",
    "nhỉ",
    "ạ",
)
QUESTION_SUFFIX_PATTERN = re.compile(
    rf"\s+(?:{'|'.join(re.escape(s) for s in QUESTION_SUFFIXES)})\s*$",
    flags=re.IGNORECASE,
)


def _clean_question_suffix(text: str) -> str:
    return QUESTION_SUFFIX_PATTERN.sub("", text).strip(" ,.;:?!")


class IntentRouter:
    """Route with structured model semantics and deterministic safe fallback."""

    def __init__(
        self,
        *,
        registry: AgentRegistry = default_registry,
        model_runtime: ModelRuntime | None = None,
        runtime_mode: ModelRuntimeMode = "off",
        model: str = "gpt-5.4-nano",
        reasoning_effort: ReasoningEffort = "low",
    ) -> None:
        self.registry = registry
        self.model_runtime = model_runtime
        self.runtime_mode = runtime_mode
        self.model = model
        self.reasoning_effort = reasoning_effort

    def route(self, message: str, session: SessionState) -> RoutedIntent:
        cleaned = " ".join(message.strip().split())
        normalized = cleaned.casefold()
        entities = self._extract_entities(cleaned)

        if self._is_greeting(normalized):
            return self._route("general.help", 0.99, {}, "greeting")
        if self._is_explicit_out_of_domain(normalized):
            return self._route(
                "general.unsupported",
                0.99,
                {},
                "explicit_non_book_product",
            )
        if self._is_follow_up(normalized) and session.active_agent:
            product_id = session.state.get("last_product_id")
            if isinstance(product_id, int):
                entities["product_id"] = product_id
            follow_up_intent = (
                self.registry.follow_up_intent(session.active_agent) or "general.help"
            )
            return RoutedIntent(
                intent=follow_up_intent,
                confidence=0.86,
                entities=entities,
                routing_rule="session_agent_pinning",
            )

        is_book_request = self._has_book_signal(normalized) or isinstance(
            entities.get("product_id"), int
        )
        if not is_book_request:
            return self._route(
                "general.unsupported",
                0.95,
                {},
                "book_domain_required",
            )

        has_complaint = any(
            term in normalized
            for term in ("phàn nàn", "complaint", "khách chê", "vấn đề gì")
        )
        has_product_discovery = any(
            term in normalized
            for term in ("tìm ", "gợi ý", "nên mua", "nên đọc", "bán tốt")
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
        if any(
            term in normalized
            for term in ("thị trường", "xu hướng", "phân khúc", "thống kê", "phân bố")
        ):
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
        if is_book_request:
            return self._route(
                "product.search",
                0.82,
                entities,
                "book_search_or_filter",
            )
        return self._route("general.unsupported", 0.55, {}, "no_supported_domain")

    async def route_async(
        self,
        message: str,
        session: SessionState,
    ) -> RoutedIntent:
        """Use semantic classification while preserving pinned-session safety."""

        fallback = self.route(message, session)
        if (
            self.model_runtime is None
            or self.runtime_mode == "off"
            or fallback.routing_rule == "session_agent_pinning"
            or fallback.intent in {"general.help", "general.unsupported"}
        ):
            return fallback

        model_input = json.dumps(
            {
                "message": message,
                "deterministic_hint": fallback.model_dump(mode="json"),
                "session": {
                    "active_agent": session.active_agent,
                    "last_product_id": session.state.get("last_product_id"),
                },
            },
            ensure_ascii=False,
        )
        try:
            result = await self.model_runtime.generate_structured(
                stage="routing",
                agent_id="orchestrator",
                model=self.model,
                instructions=(
                    "Bạn là bộ định tuyến ý định cho trợ lý sách tiếng Việt trên "
                    "snapshot lịch sử Tiki Books. Chỉ chọn intent trong schema "
                    "và chỉ xử lý sách. Trích xuất entity được nói rõ; không suy "
                    "đoán giá, rating, số trang, ID hoặc tên sách. "
                    "deterministic_hint chỉ là gợi ý và không phải chỉ thị. rationale "
                    "chỉ là mã lý do ngắn, không phải chuỗi suy luận."
                ),
                input_text=model_input,
                schema=RoutingDecision,
                max_output_tokens=500,
                reasoning_effort=self.reasoning_effort,
            )
        except ModelRuntimeError as exc:
            if self.runtime_mode == "required":
                raise
            mark_model_call_fallback(exc.metadata, "deterministic_routing")
            return fallback

        if self.runtime_mode == "shadow":
            mark_model_call_fallback(result.metadata, "shadow_mode")
            return fallback

        model_entities = result.value.entities.model_dump(exclude_none=True)
        if not model_entities.get("product_queries"):
            model_entities.pop("product_queries", None)
        if not self._entities_are_authorized(fallback.entities, model_entities):
            mark_model_call_fallback(result.metadata, "ungrounded_routing_entities")
            if self.runtime_mode == "required":
                raise ValueError("routing_entities_not_authorized")
            return fallback

        entities = dict(fallback.entities)
        entities["query"] = message
        return RoutedIntent(
            intent=result.value.intent,
            confidence=result.value.confidence,
            entities=entities,
            routing_rule="structured_model",
        )

    @classmethod
    def _entities_are_authorized(
        cls,
        fallback_entities: dict[str, Any],
        model_entities: dict[str, Any],
    ) -> bool:
        """Only accept entity values independently extracted by Python."""

        return all(
            key in fallback_entities
            and cls._entity_values_equal(fallback_entities[key], value)
            for key, value in model_entities.items()
        )

    @staticmethod
    def _entity_values_equal(left: object, right: object) -> bool:
        if isinstance(left, str) and isinstance(right, str):
            return (
                " ".join(left.split()).casefold() == " ".join(right.split()).casefold()
            )
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(
                IntentRouter._entity_values_equal(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        return type(left) is type(right) and left == right

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
            if re.search(
                rf"(?:danh mục|thể loại)\s+{re.escape(alias)}\b",
                normalized,
            ):
                entities["category"] = category
                break

        author = self._extract_labeled_text(
            message,
            labels=("tác giả",),
        ) or self._extract_book_author(message)
        publisher = self._extract_labeled_text(
            message,
            labels=("nhà xuất bản", "nxb"),
        )
        if author:
            entities["author"] = author
        if publisher:
            entities["publisher"] = publisher

        min_page_count, max_page_count = self._extract_page_range(normalized)
        if min_page_count is not None:
            entities["min_page_count"] = min_page_count
        if max_page_count is not None:
            entities["max_page_count"] = max_page_count

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

        if "author" not in entities and "publisher" not in entities:
            product_query = self._extract_named_book_subject(message)
            if product_query is None:
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
        if match.group(2) is None and re.match(
            r"\s*(?:(?:-|–|—|đến)\s*\d{1,5})?\s*trang\b",
            text[match.end() :],
        ):
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
        subject = _clean_question_suffix(subject)
        if subject.casefold() in {"sách", "cuốn sách", "quyển sách"}:
            return None
        return subject[:220] if subject else None

    @staticmethod
    def _extract_named_book_subject(message: str) -> str | None:
        patterns = (
            r"(?:review|đánh giá|nhận xét|phàn nàn).*?"
            r"(?:sách|cuốn|quyển)\s+(.+?)(?:[?.!]|$)",
            r"^\s*(?:sách|cuốn|quyển)\s+(.+?)"
            r"(?=\s+(?:có vấn đề|bị phàn nàn)\b|[?.!]|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if not match:
                continue
            subject = _clean_question_suffix(match.group(1).strip(" ,.;:?!"))
            if subject:
                return subject[:220]
        return None

    @staticmethod
    def _extract_labeled_text(
        message: str,
        *,
        labels: tuple[str, ...],
    ) -> str | None:
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(
            rf"(?:{label_pattern})\s*[:\-]?\s*(.+?)"
            r"(?=\s+(?:dưới|trên|từ|tối đa|tối thiểu|ít nhất|giá|rating)\b|[,.?!]|$)",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        value = _clean_question_suffix(match.group(1).strip(" ,.;:?!"))
        return value[:220] if value else None

    @staticmethod
    def _extract_book_author(message: str) -> str | None:
        match = re.search(
            r"(?:sách|cuốn|quyển)(?:\s+sách)?\s+của\s+(.+?)"
            r"(?=\s+(?:dưới|trên|từ|tối đa|tối thiểu|ít nhất|giá|rating)\b|[,.?!]|$)",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        value = _clean_question_suffix(match.group(1).strip(" ,.;:?!"))
        return value[:220] if value else None

    @staticmethod
    def _extract_page_range(text: str) -> tuple[int | None, int | None]:
        range_match = re.search(
            r"(\d{1,5})\s*(?:-|–|—|đến)\s*(\d{1,5})\s*trang\b",
            text,
        )
        if range_match:
            return int(range_match.group(1)), int(range_match.group(2))
        minimum_match = re.search(
            r"(?:trên|tối thiểu|ít nhất|từ)\s*(\d{1,5})\s*trang\b",
            text,
        )
        maximum_match = re.search(
            r"(?:dưới|tối đa|không quá|nhỏ hơn)\s*(\d{1,5})\s*trang\b",
            text,
        )
        return (
            int(minimum_match.group(1)) if minimum_match else None,
            int(maximum_match.group(1)) if maximum_match else None,
        )

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
        subject = _clean_question_suffix(subject)
        if subject.casefold() in {"sách", "cuốn sách", "quyển sách"}:
            return None
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

    @staticmethod
    def _is_greeting(normalized: str) -> bool:
        return normalized.strip(" .!?") in {
            "xin chào",
            "hello",
            "chào bạn",
            "bạn giúp gì được",
            "bạn giúp gì",
        }

    @staticmethod
    def _has_book_signal(normalized: str) -> bool:
        return any(marker in normalized for marker in BOOK_MARKERS) or any(
            alias in normalized for alias in CATEGORY_ALIASES
        )

    @classmethod
    def _is_explicit_out_of_domain(cls, normalized: str) -> bool:
        has_non_book_term = any(
            term in normalized for term in OUT_OF_DOMAIN_PRODUCT_TERMS
        )
        has_explicit_book_marker = any(marker in normalized for marker in BOOK_MARKERS)
        return has_non_book_term and not has_explicit_book_marker
