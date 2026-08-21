"""Optional real-LLM smoke test; excluded unless explicitly selected."""

import pytest

from app.agent.runner import run_agent
from app.core.config import settings

pytestmark = pytest.mark.integration


if not settings.openai_api_key:
    pytest.skip(
        "OPENAI_API_KEY is required for the OpenAI integration test",
        allow_module_level=True,
    )


@pytest.mark.asyncio
async def test_openai_agent_can_call_search_tool() -> None:
    result = await run_agent(
        message="Tìm tai nghe dưới 1 triệu rating ít nhất 4.5",
        session_id="integration-search",
        request_id="integration-search",
    )

    assert result.answer.strip()
    assert any(call.name == "search_products" for call in result.tool_calls)
