from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def validation_client() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                _env_file=None,
                app_env="test",
                gateway_api_keys="test:test-secret-key",
                gateway_principal_policies="test:default:ecommerce.read",
            )
        )
    )


def test_chat_rejects_empty_message() -> None:
    response = validation_client().post(
        "/api/v2/chat",
        headers={"X-API-Key": "test-secret-key"},
        json={
            "conversation_id": "conversation_test_123",
            "client_turn_id": "turn-001",
            "message": "",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "v2.validation_failed"
    assert any(
        error["field"] == "body.message"
        for error in response.json()["error"]["validation_errors"]
    )


def test_chat_rejects_whitespace_only_message() -> None:
    response = validation_client().post(
        "/api/v2/chat",
        headers={"X-API-Key": "test-secret-key"},
        json={
            "conversation_id": "conversation_test_123",
            "client_turn_id": "turn-002",
            "message": "   ",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "v2.validation_failed"
