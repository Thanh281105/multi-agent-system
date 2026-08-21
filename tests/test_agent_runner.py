"""Deterministic ADK runtime smoke test without a network model call."""

from collections.abc import AsyncGenerator

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm, LlmCapabilities
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent import runner as runner_module
from app.core.config import settings


class DeterministicToolCallingModel(BaseLlm):
    """Emit one tool call, then a final answer, without external I/O."""

    calls: int = 0

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        del llm_request, stream
        self.calls += 1
        if self.calls == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="search_products",
                            args={
                                "category": "Tai nghe",
                                "max_price": 1_000_000,
                                "min_rating": 4.5,
                            },
                        )
                    ],
                ),
                partial=False,
            )
            return

        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="Có 2 sản phẩm phù hợp từ dữ liệu database."
                    )
                ],
            ),
            partial=False,
        )


class DeterministicMultiToolModel(BaseLlm):
    """Emit a search call, a review call, and then a final answer."""

    calls: int = 0

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        del llm_request, stream
        self.calls += 1
        if self.calls == 1:
            part = types.Part.from_function_call(
                name="search_products",
                args={"query": "Nova Air S2"},
            )
        elif self.calls == 2:
            part = types.Part.from_function_call(
                name="get_product_reviews",
                args={"product_id": 1},
            )
        else:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text="Nova Air S2 có review tích cực nhưng có nhược điểm."
                        )
                    ],
                ),
                partial=False,
            )
            return

        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            partial=False,
        )


@pytest.mark.asyncio
async def test_adk_runner_dispatches_tool_and_collects_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = DeterministicToolCallingModel(model="fake-tool-model")
    fake_agent = LlmAgent(
        name="test_ecommerce_agent",
        model=model,
        instruction="Use the registered search tool and answer from its facts.",
        tools=[runner_module.root_agent.tools[0]],
    )
    fake_runner = InMemoryRunner(
        agent=fake_agent,
        app_name=settings.adk_app_name,
    )
    monkeypatch.setattr(runner_module, "runner", fake_runner)

    result = await runner_module.run_agent(
        message="Tìm tai nghe dưới 1 triệu rating ít nhất 4.5",
        session_id="deterministic-runner-test",
        request_id="deterministic-runner-test",
    )

    assert result.answer == "Có 2 sản phẩm phù hợp từ dữ liệu database."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_products"
    assert result.tool_calls[0].arguments["max_price"] == 1_000_000
    assert result.tool_calls[0].result_summary == {
        "count": 2,
        "products_count": 2,
    }


@pytest.mark.asyncio
async def test_adk_runner_exposes_multiple_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = DeterministicMultiToolModel(model="fake-multi-tool-model")
    fake_agent = LlmAgent(
        name="test_multi_tool_agent",
        model=model,
        instruction="Use tools in sequence and answer from their facts.",
        tools=list(runner_module.root_agent.tools[:2]),
    )
    fake_runner = InMemoryRunner(
        agent=fake_agent,
        app_name=settings.adk_app_name,
    )
    monkeypatch.setattr(runner_module, "runner", fake_runner)

    result = await runner_module.run_agent(
        message="Đọc review của Nova Air S2",
        session_id="deterministic-multi-tool-test",
        request_id="deterministic-multi-tool-test",
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
