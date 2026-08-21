import pytest
from fastapi.testclient import TestClient

from app.agent.runner import AgentRunError
from app.main import app


@pytest.mark.asyncio
async def test_chat_returns_stable_error_when_adk_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_run_agent(**_: object) -> None:
        raise AgentRunError("simulated ADK failure")

    import app.api.chat as chat_api

    monkeypatch.setattr(chat_api, "run_agent", fail_run_agent)
    response = TestClient(app).post("/chat", json={"message": "Xin chào"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Không thể xử lý yêu cầu lúc này."}
