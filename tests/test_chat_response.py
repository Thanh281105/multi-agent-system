import pytest
from fastapi.testclient import TestClient

from app.agent.runner import AgentRunResult
from app.core.config import Settings
from app.main import create_app
from app.schemas.chat import ToolCallInfo


@pytest.mark.asyncio
async def test_chat_exposes_runner_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_agent(**_: object) -> AgentRunResult:
        return AgentRunResult(
            answer="Em tìm thấy dữ liệu phù hợp.",
            tool_calls=[
                ToolCallInfo(
                    name="search_products",
                    arguments={"author": "Trang Anh", "max_page_count": 610},
                    result_summary={"count": 1},
                )
            ],
        )

    import app.api.chat as chat_api

    monkeypatch.setattr(chat_api, "run_agent", fake_run_agent)
    application = create_app(
        Settings(_env_file=None, app_env="test", legacy_chat_enabled=True)
    )
    response = TestClient(application).post(
        "/chat",
        json={"message": "Tìm sách của Trang Anh"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["answer"] == "Em tìm thấy dữ liệu phù hợp."
    assert body["tool_calls"][0]["name"] == "search_products"
    assert body["tool_calls"][0]["arguments"]["max_page_count"] == 610
    assert body["session_id"].startswith("sess_")
