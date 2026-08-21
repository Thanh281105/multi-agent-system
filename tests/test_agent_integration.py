"""Optional real-LLM smoke test; excluded unless explicitly selected."""

import os

import pytest

from app.agent.runner import run_agent

pytestmark = pytest.mark.integration


if not os.getenv("GOOGLE_API_KEY") and not os.getenv("GEMINI_API_KEY"):
    pytest.skip(
        "GOOGLE_API_KEY is required for the ADK integration test",
        allow_module_level=True,
    )


@pytest.mark.asyncio
async def test_adk_agent_can_call_search_tool() -> None:
    result = await run_agent(
        message="Tìm tai nghe dưới 1 triệu rating ít nhất 4.5",
        session_id="integration-search",
        request_id="integration-search",
    )

    assert result.answer.strip()
    assert any(call.name == "search_products" for call in result.tool_calls)
