"""Deterministic OpenAI runner smoke tests without network model calls."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import runner as runner_module
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
                            "category": "Tai nghe",
                            "max_price": 1_000_000,
                            "min_rating": 4.5,
                        },
                        call_id="call-search",
                    )
                ],
            ),
            response(
                response_id="resp-search-final",
                output=[],
                output_text="Có 2 sản phẩm phù hợp từ dữ liệu database.",
            ),
        ]
    )
    monkeypatch.setattr(runner_module, "openai_client", client)

    result = await runner_module.run_agent(
        message="Tìm tai nghe dưới 1 triệu rating ít nhất 4.5",
        session_id="openai-deterministic-search",
        request_id="openai-deterministic-search",
    )

    assert result.answer == "Có 2 sản phẩm phù hợp từ dữ liệu database."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_products"
    assert result.tool_calls[0].arguments["max_price"] == 1_000_000
    assert result.tool_calls[0].result_summary == {
        "count": 2,
        "products_count": 2,
    }
    assert client.responses.requests[0]["model"] == settings.openai_model
    assert client.responses.requests[1]["previous_response_id"] == "resp-search-tool"
    assert client.responses.requests[1]["input"][0]["call_id"] == "call-search"


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
                        arguments={"query": "Nova Air S2"},
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
                output_text="Nova Air S2 có review tích cực nhưng có nhược điểm.",
            ),
        ]
    )
    monkeypatch.setattr(runner_module, "openai_client", client)

    result = await runner_module.run_agent(
        message="Đọc review của Nova Air S2",
        session_id="openai-deterministic-multi",
        request_id="openai-deterministic-multi",
    )

    assert result.answer == "Nova Air S2 có review tích cực nhưng có nhược điểm."
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
    assert [item["call_id"] for item in client.responses.requests[1]["input"]] == [
        "call-product",
        "call-reviews",
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
