"""Grounded Vietnamese response aggregation over typed agent results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.contracts import AgentResult, DataProvenance, TaskStatus
from app.orchestrator.scoring import score_recommendation_candidates


@dataclass(frozen=True, slots=True)
class Aggregation:
    status: TaskStatus
    answer: str
    warnings: tuple[str, ...]
    provenance: tuple[DataProvenance, ...]
    selected_product_id: int | None = None


class ResultAggregator:
    """Generate concise answers exclusively from agent-returned facts."""

    def aggregate(
        self,
        *,
        intent: str,
        results: tuple[AgentResult, ...],
    ) -> Aggregation:
        status = self._status(results)
        warnings = tuple(error.message for result in results for error in result.errors)
        provenance = self._provenance(results)
        selected_product_id: int | None = None

        if intent == "general.help":
            answer = (
                "Mình có thể tìm/so sánh sản phẩm, phân tích review và complaint, "
                "hoặc tổng hợp tín hiệu thị trường từ bộ dữ liệu mẫu."
            )
        elif intent == "general.unsupported":
            answer = (
                "Yêu cầu này nằm ngoài các miền sản phẩm, review, độ tin cậy và "
                "thị trường mà hệ thống hiện hỗ trợ; mình chưa gọi tool để tránh "
                "tạo thông tin không có nguồn."
            )
        elif intent == "multi.recommendation":
            answer, selected_product_id = self._multi_recommendation(results)
        elif intent.startswith("product"):
            answer = self._product_answer(results)
        elif intent.startswith("review"):
            answer = self._review_answer(results)
        elif intent.startswith("trust"):
            answer = self._trust_answer(results)
        elif intent.startswith("market"):
            answer = self._market_answer(results)
        else:
            answer = "Chưa có dữ liệu phù hợp để trả lời yêu cầu này."

        if status == TaskStatus.PARTIAL_SUCCESS:
            answer += (
                " Một phần dữ liệu không truy xuất được; kết quả trên chỉ dùng "
                "phần còn lại."
            )
        elif status == TaskStatus.FAILED and warnings:
            answer = "Không thể hoàn tất yêu cầu từ các nguồn dữ liệu hiện có."
        return Aggregation(
            status,
            answer,
            warnings,
            provenance,
            selected_product_id,
        )

    @staticmethod
    def _status(results: tuple[AgentResult, ...]) -> TaskStatus:
        if not results:
            return TaskStatus.SUCCESS
        if all(result.status == TaskStatus.FAILED for result in results):
            return TaskStatus.FAILED
        if any(result.status != TaskStatus.SUCCESS for result in results):
            return TaskStatus.PARTIAL_SUCCESS
        return TaskStatus.SUCCESS

    def _product_answer(self, results: tuple[AgentResult, ...]) -> str:
        comparison = self._first_data(results, "requested_product_ids")
        if comparison:
            products = comparison.get("products", [])
            if not products:
                return "Không tìm thấy sản phẩm để so sánh trong dữ liệu mẫu."
            lines = [self._product_line(product) for product in products]
            return "So sánh theo dữ liệu mẫu: " + "; ".join(lines) + "."

        ranked = self._first_nested(results, "ranking", "products")
        products = ranked or self._first_list(results, "products")
        if not products:
            return "Không tìm thấy sản phẩm phù hợp trong dữ liệu mẫu."
        lines = [self._product_line(product) for product in products[:5]]
        return "Các sản phẩm phù hợp theo dữ liệu mẫu: " + "; ".join(lines) + "."

    def _review_answer(self, results: tuple[AgentResult, ...]) -> str:
        review = self._agent_data(results, "review_agent")
        if review and isinstance(review.get("summary"), str):
            product = self._retrieved_product(review)
            prefix = f"{product['name']}: " if product else ""
            return prefix + str(review["summary"])
        if review:
            retrieval = review.get("retrieval")
            if isinstance(retrieval, dict) and not retrieval.get("found", True):
                return (
                    "Không tìm thấy sản phẩm hoặc review tương ứng trong dữ liệu mẫu."
                )
        return "Không tìm thấy sản phẩm để phân tích review trong dữ liệu mẫu."

    def _trust_answer(self, results: tuple[AgentResult, ...]) -> str:
        trust = self._agent_data(results, "trust_agent")
        if not trust:
            return "Không tìm thấy sản phẩm để phân tích complaint trong dữ liệu mẫu."
        complaints = trust.get("complaints")
        trust_data = trust.get("trust")
        if not isinstance(complaints, dict):
            return "Có review nhưng chưa tính được chỉ số complaint."
        product = self._retrieved_product(trust)
        name = product["name"] if product else "Sản phẩm"
        rate = round(float(complaints.get("complaint_rate", 0)) * 100)
        count = int(complaints.get("complaint_count", 0))
        answer = f"{name} có {count} review mang tín hiệu complaint ({rate}%)."
        if isinstance(trust_data, dict):
            score = round(float(trust_data.get("average_trust_score", 0)) * 100)
            answer += f" Điểm tin cậy heuristic trung bình là {score}%."
        return answer

    def _market_answer(self, results: tuple[AgentResult, ...]) -> str:
        market_agent = self._agent_data(results, "market_agent")
        if not market_agent:
            return "Chưa truy xuất được tín hiệu thị trường mẫu."
        market = market_agent.get("market")
        if isinstance(market, dict):
            statistics = market.get("statistics", {})
            count = int(statistics.get("product_count", 0))
            average_price = statistics.get("average_price")
            average_rating = statistics.get("average_rating")
            return (
                f"Bộ dữ liệu mẫu có {count} sản phẩm trong phạm vi đã chọn, "
                f"giá trung bình {self._price(average_price)} và rating trung bình "
                f"{average_rating}. Đây không phải thống kê đại diện thị trường thật."
            )
        knowledge = market_agent.get("knowledge")
        if isinstance(knowledge, dict) and knowledge.get("documents"):
            document = knowledge["documents"][0]
            return (
                f"Ghi chú dữ liệu mẫu: {document['content']} "
                "Đây không phải báo cáo thị trường thật."
            )
        return "Không tìm thấy ghi chú thị trường phù hợp trong dữ liệu mẫu."

    def _multi_recommendation(
        self,
        results: tuple[AgentResult, ...],
    ) -> tuple[str, int | None]:
        ranked = self._first_nested(results, "ranking", "products")
        if not ranked:
            return (
                "Không tìm thấy sản phẩm phù hợp để tạo recommendation có căn cứ.",
                None,
            )
        scoring = score_recommendation_candidates(
            ranked,
            self._agent_analyses(results, "review_agent"),
            self._agent_analyses(results, "trust_agent"),
        )
        candidates = scoring["candidates"]
        if not candidates:
            return "Không đủ tín hiệu có căn cứ để chấm điểm recommendation.", None
        product = candidates[0]
        score = round(float(product["multi_agent_score"]) * 100)
        coverage = round(float(product["signal_coverage"]) * 100)
        answer = (
            f"Đã so sánh {len(candidates)} ứng viên. Đề xuất đứng đầu theo dữ liệu "
            "mẫu là "
            + self._product_line(product)
            + f", với điểm đa agent {score}% và độ phủ tín hiệu {coverage}%. "
            "Công thức: Product 55%, cảm xúc review 15%, ít complaint 20%, "
            "độ tin cậy review 10%; tín hiệu thiếu ở bất kỳ ứng viên nào sẽ "
            "được bỏ cho cả nhóm rồi chuẩn hóa lại trọng số."
        )
        complaint_rate = product.get("complaint_rate")
        if isinstance(complaint_rate, (int, float)) and not isinstance(
            complaint_rate, bool
        ):
            rate = round(float(complaint_rate) * 100)
            answer += f" Tỷ lệ review có tín hiệu complaint là {rate}%."
        sentiment = product.get("positive_sentiment")
        if isinstance(sentiment, (int, float)) and not isinstance(sentiment, bool):
            answer += (
                f" Tỷ lệ review được phân loại tích cực là "
                f"{round(float(sentiment) * 100)}%."
            )
        product_id = product.get("id")
        selected_product_id = (
            int(product_id)
            if isinstance(product_id, int) and not isinstance(product_id, bool)
            else None
        )
        return answer, selected_product_id

    @staticmethod
    def _product_line(product: dict[str, Any]) -> str:
        return (
            f"{product.get('name')} — {ResultAggregator._price(product.get('price'))}, "
            f"rating {product.get('rating')}, đã bán {product.get('sold_count')}"
        )

    @staticmethod
    def _price(value: object) -> str:
        if value is None:
            return "chưa có giá"
        if isinstance(value, bool):
            return "chưa có giá"
        if isinstance(value, (int, float)):
            amount = int(value)
        elif isinstance(value, str):
            try:
                amount = int(float(value))
            except ValueError:
                return "chưa có giá"
        else:
            return "chưa có giá"
        return f"{amount:,}₫".replace(",", ".")

    @staticmethod
    def _agent_data(
        results: tuple[AgentResult, ...],
        agent_id: str,
    ) -> dict[str, Any] | None:
        for result in reversed(results):
            if result.agent_id == agent_id and result.status != TaskStatus.FAILED:
                return result.data
        return None

    @classmethod
    def _agent_analyses(
        cls,
        results: tuple[AgentResult, ...],
        agent_id: str,
    ) -> list[dict[str, Any]]:
        data = cls._agent_data(results, agent_id)
        if not data:
            return []
        analyses = data.get("analyses")
        if not isinstance(analyses, list):
            return []
        return [item for item in analyses if isinstance(item, dict)]

    @staticmethod
    def _first_data(
        results: tuple[AgentResult, ...],
        key: str,
    ) -> dict[str, Any] | None:
        for result in reversed(results):
            if key in result.data:
                return result.data
        return None

    @staticmethod
    def _first_list(
        results: tuple[AgentResult, ...],
        key: str,
    ) -> list[dict[str, Any]]:
        for result in reversed(results):
            value = result.data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _first_nested(
        results: tuple[AgentResult, ...],
        outer: str,
        inner: str,
    ) -> list[dict[str, Any]]:
        for result in reversed(results):
            outer_value = result.data.get(outer)
            if isinstance(outer_value, dict):
                inner_value = outer_value.get(inner)
                if isinstance(inner_value, list):
                    return [item for item in inner_value if isinstance(item, dict)]
        return []

    @staticmethod
    def _retrieved_product(data: dict[str, Any]) -> dict[str, Any] | None:
        retrieval = data.get("retrieval")
        if not isinstance(retrieval, dict):
            return None
        product = retrieval.get("product")
        return product if isinstance(product, dict) else None

    @staticmethod
    def _provenance(results: tuple[AgentResult, ...]) -> tuple[DataProvenance, ...]:
        unique: dict[tuple[str, str], DataProvenance] = {}
        for result in results:
            for record in result.provenance:
                unique[(record.source_type, record.source_id)] = record
        return tuple(unique.values())
