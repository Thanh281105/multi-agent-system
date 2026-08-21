from fastapi.testclient import TestClient

from app.main import app


def test_chat_rejects_empty_message() -> None:
    response = TestClient(app).post("/chat", json={"message": ""})

    assert response.status_code == 422
    assert "message" in response.text


def test_chat_rejects_whitespace_only_message() -> None:
    response = TestClient(app).post("/chat", json={"message": "   "})

    assert response.status_code == 422
