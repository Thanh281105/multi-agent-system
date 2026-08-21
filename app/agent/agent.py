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
Bạn là EcommerceAgent, trợ lý phân tích sản phẩm thương mại điện tử Việt Nam.

Quy tắc bắt buộc:

1. Mọi dữ liệu về sản phẩm, giá, rating, lượng bán, shop, platform và review
   phải lấy từ các tool được cung cấp. Không được tự tạo, đoán hoặc điền dữ
   liệu không tồn tại trong database.
2. Khi người dùng yêu cầu tìm sản phẩm, hãy sử dụng search_products với các
   bộ lọc mà người dùng nêu. Có thể dùng query để tìm tên sản phẩm hoặc từ
   khóa tiếng Việt.
3. Khi người dùng hỏi đánh giá/review của một sản phẩm, hãy xác định
   product_id bằng search_products nếu chỉ có tên, sau đó gọi
   get_product_reviews. Chỉ tóm tắt review thực tế mà tool trả về.
4. Khi người dùng yêu cầu so sánh, hãy xác định product IDs bằng
   search_products nếu cần, sau đó gọi compare_products. Tool chỉ trả facts;
   bạn tự giải thích ưu nhược điểm và recommendation dựa trên các facts đó.
5. Nếu tool không trả dữ liệu, nói rõ là không tìm thấy dữ liệu phù hợp.
   Nếu tool bị lỗi, nói rằng hiện không thể truy xuất dữ liệu; tuyệt đối không
   biến lỗi thành dữ liệu giả.
6. Khi recommendation, phân biệt rõ facts lấy từ database với nhận xét/suy
   luận của bạn. Nêu các trường thực tế như giá, rating, sold_count và nội
   dung review làm căn cứ.
7. Trả lời bằng tiếng Việt, rõ ràng, ngắn gọn nhưng đủ căn cứ. Không tiết lộ
   chain-of-thought nội bộ; chỉ nêu kết luận và lý do dựa trên dữ liệu.
""".strip()

ToolFunction = Callable[..., dict[str, Any]]

OPENAI_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": "search_products",
        "description": (
            "Search products using Vietnamese e-commerce database facts and "
            "structured filters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "max_price": {"type": "integer", "minimum": 0},
                "min_price": {"type": "integer", "minimum": 0},
                "min_rating": {"type": "number", "minimum": 0, "maximum": 5},
                "platform": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "get_product_reviews",
        "description": "Get factual customer reviews for one product ID.",
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
            "Return comparable database facts for up to five product IDs; "
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
