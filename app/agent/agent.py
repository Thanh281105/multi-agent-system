"""OpenAI Responses API agent definition for the Phase 1 proof of concept."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.tools.ecommerce import (
    compare_products,
    get_product_reviews,
    search_products,
)

AGENT_INSTRUCTION = """
Bạn là TikiBooksAgent, trợ lý phân tích sách tiếng Việt từ snapshot lịch sử Tiki.

Quy tắc bắt buộc:

1. Mọi dữ liệu về sách, tác giả, nhà xuất bản, số trang, giá, rating, độ phổ
   biến do nguồn ghi nhận và review
   phải lấy từ các tool được cung cấp. Không được tự tạo, đoán hoặc điền dữ
   liệu không tồn tại trong database.
2. Khi người dùng yêu cầu tìm sách, hãy sử dụng search_products với title,
   author, publisher, category, price, rating và page count mà họ nêu.
3. Khi người dùng hỏi đánh giá/review của một cuốn sách, hãy xác định
   product_id bằng search_products nếu chỉ có tên, sau đó gọi
   get_product_reviews. Chỉ tóm tắt review thực tế mà tool trả về.
4. Khi người dùng yêu cầu so sánh, hãy xác định product IDs bằng
   search_products nếu cần, sau đó gọi compare_products. Tool chỉ trả facts;
   bạn tự giải thích ưu nhược điểm và recommendation dựa trên các facts đó.
5. Nếu tool không trả dữ liệu, nói rõ là không tìm thấy dữ liệu phù hợp.
   Nếu tool bị lỗi, nói rằng hiện không thể truy xuất dữ liệu; tuyệt đối không
   biến lỗi thành dữ liệu giả.
6. Khi recommendation, phân biệt rõ facts lấy từ database với nhận xét/suy
   luận. Không gọi sold_count là doanh số đã xác minh; chỉ gọi là độ phổ biến
   do nguồn ghi nhận.
7. Chỉ hỗ trợ miền sách. Với sản phẩm ngoài sách, nói rõ yêu cầu chưa được hỗ
   trợ và không gọi tool để tạo dữ liệu ngoài miền.
8. Mọi câu trả lời dùng dữ liệu phải nêu: dữ liệu là snapshot lịch sử Tiki
   Books phục vụ đồ án, không phản ánh catalog, giá hoặc tồn kho hiện tại.
9. Trả lời bằng tiếng Việt, rõ ràng, ngắn gọn nhưng đủ căn cứ. Không tiết lộ
   chain-of-thought nội bộ; chỉ nêu kết luận và lý do dựa trên dữ liệu.
""".strip()

ToolFunction = Callable[..., dict[str, Any]]

OPENAI_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": "search_products",
        "description": ("Search historical Tiki book facts with structured filters."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "max_price": {"type": "integer", "minimum": 0},
                "min_price": {"type": "integer", "minimum": 0},
                "min_rating": {"type": "number", "minimum": 0, "maximum": 5},
                "author": {"type": "string"},
                "publisher": {"type": "string"},
                "min_page_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20000,
                },
                "max_page_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20000,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "get_product_reviews",
        "description": "Get cleaned sampled customer reviews for one book ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["product_id"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "compare_products",
        "description": (
            "Return comparable historical facts for up to five book IDs; "
            "the agent makes the explanation and recommendation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 1,
                    "maxItems": 5,
                }
            },
            "required": ["product_ids"],
            "additionalProperties": False,
        },
        "strict": False,
    },
)

TOOL_FUNCTIONS: Mapping[str, ToolFunction] = {
    "search_products": search_products,
    "get_product_reviews": get_product_reviews,
    "compare_products": compare_products,
}


@dataclass(frozen=True)
class AgentDefinition:
    """Provider-neutral metadata used by the OpenAI runner boundary."""

    name: str
    instructions: str
    tools: tuple[dict[str, Any], ...]
    functions: Mapping[str, ToolFunction]


ecommerce_agent = AgentDefinition(
    name="ecommerce_agent",
    instructions=AGENT_INSTRUCTION,
    tools=OPENAI_TOOLS,
    functions=TOOL_FUNCTIONS,
)

# Keep the conventional root-agent name for future CLI/deployment wrappers.
root_agent = ecommerce_agent
