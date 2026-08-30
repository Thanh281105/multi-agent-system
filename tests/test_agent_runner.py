"""Deterministic OpenAI runner smoke tests without network model calls."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import runner as runner_module
from app.agent.agent import AGENT_INSTRUCTION, OPENAI_TOOLS
from app.core.config import settings


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        return self._responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = FakeResponses(responses)


def function_call(*, name: str, arguments: dict[str, Any], call_id: str) -> Any:
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments=json.dumps(arguments),
        call_id=call_id,
    )


def response(*, response_id: str, output: list[Any], output_text: str = "") -> Any:
    return SimpleNamespace(
        id=response_id,
        output=output,
        output_text=output_text,
    )


@pytest.mark.asyncio
async def test_openai_runner_dispatches_tool_and_collects_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeOpenAIClient(
        [
            response(
                response_id="resp-search-tool",
                output=[
                    function_call(
                        name="search_products",
                        arguments={
                            "author": "Trang Anh",
                            "min_page_count": 600,
                            "max_page_count": 610,
                        },
                        call_id="call-search",
                    )
                ],
            ),
            response(
                response_id="resp-search-final",
                output=[],
                output_text=("Có 1 sách phù hợp trong snapshot lịch sử Tiki Books."),
            ),
        ]
    )
    monkeypatch.setattr(runner_module, "openai_client", client)
    monkeypatch.setattr(runner_module, "session_histories", {})

    result = await runner_module.run_agent(
        message="Tìm sách của Trang Anh từ 600 đến 610 trang",
        session_id="openai-deterministic-search",
        request_id="openai-deterministic-search",
    )

    assert result.answer == "Có 1 sách phù hợp trong snapshot lịch sử Tiki Books."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_products"
    assert result.tool_calls[0].arguments["author"] == "Trang Anh"
    assert result.tool_calls[0].result_summary == {
        "count": 1,
        "products_count": 1,
    }
    assert client.responses.requests[0]["model"] == settings.openai_model
    assert all(request["store"] is False for request in client.responses.requests)
    assert "previous_response_id" not in client.responses.requests[1]
    assert client.responses.requests[1]["input"][1]["call_id"] == "call-search"
    assert client.responses.requests[1]["input"][2]["call_id"] == "call-search"


@pytest.mark.asyncio
async def test_openai_runner_exposes_multiple_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeOpenAIClient(
        [
            response(
                response_id="resp-multi-tool",
                output=[
                    function_call(
                        name="search_products",
                        arguments={"query": "Nhật Ký Tarot"},
                        call_id="call-product",
                    ),
                    function_call(
                        name="get_product_reviews",
                        arguments={"product_id": 1},
                        call_id="call-reviews",
                    ),
                ],
            ),
            response(
                response_id="resp-multi-final",
                output=[],
                output_text="Nhật Ký Tarot có review tích cực và complaint giao hàng.",
            ),
        ]
    )
    monkeypatch.setattr(runner_module, "openai_client", client)
    monkeypatch.setattr(runner_module, "session_histories", {})

    result = await runner_module.run_agent(
        message="Đọc review sách Nhật Ký Tarot",
        session_id="openai-deterministic-multi",
        request_id="openai-deterministic-multi",
    )

    assert result.answer == ("Nhật Ký Tarot có review tích cực và complaint giao hàng.")
    assert [call.name for call in result.tool_calls] == [
        "search_products",
        "get_product_reviews",
    ]
    assert result.tool_calls[1].arguments == {"product_id": 1}
    assert result.tool_calls[1].result_summary == {
        "found": True,
        "count": 5,
        "reviews_count": 5,
    }
    assert all(request["store"] is False for request in client.responses.requests)
    assert "previous_response_id" not in client.responses.requests[1]
    assert [
        item["call_id"]
        for item in client.responses.requests[1]["input"]
        if item.get("type") == "function_call_output"
    ] == ["call-product", "call-reviews"]


@pytest.mark.asyncio
async def test_openai_runner_reuses_local_history_without_response_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeOpenAIClient(
        [
            response(
                response_id="resp-follow-up-one",
                output=[],
                output_text="Sapiens có 600 trang trong snapshot.",
            ),
            response(
                response_id="resp-follow-up-two",
                output=[],
                output_text="Mình vừa giữ ngữ cảnh của câu hỏi trước.",
            ),
        ]
    )
    monkeypatch.setattr(runner_module, "openai_client", client)
    monkeypatch.setattr(runner_module, "session_histories", {})

    first = await runner_module.run_agent(
        message="Sách Sapiens có bao nhiêu trang?",
        session_id="openai-local-history",
        request_id="openai-local-history-one",
    )
    second = await runner_module.run_agent(
        message="Ý mình là trong điều kiện dùng thực tế.",
        session_id="openai-local-history",
        request_id="openai-local-history-two",
    )

    assert first.answer == "Sapiens có 600 trang trong snapshot."
    assert second.answer == "Mình vừa giữ ngữ cảnh của câu hỏi trước."
    assert all(request["store"] is False for request in client.responses.requests)
    assert all(
        "previous_response_id" not in request for request in client.responses.requests
    )
    assert client.responses.requests[1]["input"] == [
        {"role": "user", "content": "Sách Sapiens có bao nhiêu trang?"},
        {"role": "assistant", "content": "Sapiens có 600 trang trong snapshot."},
        {"role": "user", "content": "Ý mình là trong điều kiện dùng thực tế."},
    ]


@pytest.mark.asyncio
async def test_openai_runner_requires_api_key_without_injected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "openai_client", None)
    monkeypatch.setattr(runner_module.settings, "openai_api_key", None)

    with pytest.raises(runner_module.AgentRunError, match="OPENAI_API_KEY"):
        await runner_module.run_agent(
            message="Xin chào",
            session_id="openai-missing-key",
            request_id="openai-missing-key",
        )


def test_legacy_agent_prompt_and_tool_schema_are_books_only() -> None:
    search_tool = next(
        tool for tool in OPENAI_TOOLS if tool["name"] == "search_products"
    )
    properties = search_tool["parameters"]["properties"]

    assert "snapshot lịch sử Tiki" in AGENT_INSTRUCTION
    assert "Chỉ hỗ trợ miền sách" in AGENT_INSTRUCTION
    assert {"author", "publisher", "min_page_count", "max_page_count"}.issubset(
        properties
    )
    assert "platform" not in properties
