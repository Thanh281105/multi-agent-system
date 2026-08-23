"""HTTP-level security, correlation, failure, and SSE contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.contracts import ExecutionPlan, TaskStatus
from app.core.config import Settings
from app.main import create_app
from app.orchestrator import OrchestrationResult

ALICE_HEADERS = {"X-API-Key": "alice-secret-key"}
BOB_HEADERS = {"X-API-Key": "bob-secret-key"}


def test_gateway_authenticates_and_returns_grounded_correlated_result() -> None:
    application = create_app(build_test_settings())

    response = TestClient(application).post(
        "/api/v1/chat",
        headers={
            **ALICE_HEADERS,
            "X-Request-ID": "req_client_safe_123",
            "X-Trace-ID": "trace_client_must_not_win",
        },
        json={"message": "Tìm tai nghe dưới 1 triệu"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["api_version"] == "v1"
    assert body["status"] == "success"
    assert body["session_id"].startswith("sess_")
    assert body["request_id"] == "req_client_safe_123"
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert response.headers["X-Trace-ID"] == body["trace_id"]
    assert body["trace_id"] != "trace_client_must_not_win"
    assert body["executions"][0]["agent_id"] == "product_agent"
    assert body["sample_data"] is True
    assert body["provenance"]
    assert response.headers["X-RateLimit-Limit"] == "10"
    assert response.headers["X-RateLimit-Remaining"] == "9"
    assert "ranking" not in response.text


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "not-a-valid-key"}])
def test_gateway_rejects_missing_or_invalid_credentials_without_state(
    headers: dict[str, str],
) -> None:
    application = create_app(build_test_settings())
    runtime = application.state.gateway_runtime

    response = TestClient(application).post(
        "/api/v1/chat",
        headers=headers,
        json={"message": "Tìm laptop"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "gateway.authentication_failed"
    assert response.headers["WWW-Authenticate"] == "ApiKey"
    assert response.headers["X-Request-ID"].startswith("req_")
    assert response.headers["X-Trace-ID"].startswith("trace_")
    assert runtime.orchestrator.sessions.count() == 0


def test_gateway_throttles_repeated_authentication_failures() -> None:
    application = create_app(build_test_settings(gateway_auth_attempt_requests=1))
    client = TestClient(application)

    first = client.post(
        "/api/v1/chat",
        headers={"X-API-Key": "wrong-key-one"},
        json={"message": "Tìm laptop"},
    )
    throttled = client.post(
        "/api/v1/chat",
        headers={"X-API-Key": "wrong-key-two"},
        json={"message": "Tìm laptop"},
    )

    assert first.status_code == 401
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == ("gateway.authentication_rate_limited")
    assert throttled.headers["Retry-After"] == "60"


def test_validation_error_is_stable_and_does_not_echo_user_content() -> None:
    application = create_app(build_test_settings())
    secret_message = "PRIVATE-CONTENT-MUST-NOT-ECHO"

    response = TestClient(application).post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": secret_message, "session_id": "client-chosen"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "gateway.validation_failed"
    assert secret_message not in response.text
    assert response.headers["X-Request-ID"].startswith("req_")
    assert response.json()["error"]["trace_id"] == response.headers["X-Trace-ID"]


def test_unknown_and_cross_principal_sessions_are_indistinguishable() -> None:
    application = create_app(build_test_settings())
    client = TestClient(application)

    unknown = client.post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Tìm laptop", "session_id": "sess_unknown_123"},
    )
    assert application.state.gateway_runtime.orchestrator.sessions.count() == 0
    first = client.post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Tìm tai nghe dưới 1 triệu"},
    )
    session_id = first.json()["session_id"]
    continued = client.post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Còn pin thì sao?", "session_id": session_id},
    )
    foreign = client.post(
        "/api/v1/chat",
        headers=BOB_HEADERS,
        json={"message": "Còn pin thì sao?", "session_id": session_id},
    )

    assert unknown.status_code == 404
    assert first.status_code == 200
    assert continued.status_code == 200
    assert foreign.status_code == 404
    assert unknown.json()["error"]["code"] == "gateway.session_not_found"
    assert foreign.json()["error"]["code"] == "gateway.session_not_found"


def test_rate_limit_is_per_principal_and_returns_retry_metadata() -> None:
    application = create_app(build_test_settings(gateway_rate_limit_requests=1))
    client = TestClient(application)

    first = client.post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Xin chào"},
    )
    limited = client.post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Xin chào lần nữa"},
    )
    other_principal = client.post(
        "/api/v1/chat",
        headers=BOB_HEADERS,
        json={"message": "Xin chào"},
    )

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "gateway.rate_limited"
    assert limited.headers["Retry-After"] == "60"
    assert limited.headers["X-RateLimit-Remaining"] == "0"
    assert other_principal.status_code == 200


def test_all_agent_failure_maps_to_sanitized_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(build_test_settings())
    runtime = application.state.gateway_runtime

    async def failed_run(**kwargs: Any) -> OrchestrationResult:
        return OrchestrationResult(
            status=TaskStatus.FAILED,
            answer="internal detail must not be returned",
            request_id=kwargs["request_id"],
            trace_id=kwargs["trace_id"],
            session_id="sess_failed_123",
            intent="product.search",
            plan=ExecutionPlan(
                plan_id="plan_failed_123",
                intent="product.search",
            ),
            warnings=("downstream private failure",),
            duration_ms=1,
        )

    monkeypatch.setattr(runtime.orchestrator, "run", failed_run)
    response = TestClient(application).post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Tìm laptop"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gateway.all_agents_failed"
    assert "internal detail" not in response.text
    assert "private failure" not in response.text


def test_orchestration_timeout_is_cancelled_and_correlated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = build_test_settings().model_copy(
        update={"orchestration_timeout_seconds": 0.01}
    )
    application = create_app(config)
    runtime = application.state.gateway_runtime
    cancelled = asyncio.Event()

    async def slow_run(**_: Any) -> OrchestrationResult:
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(runtime.orchestrator, "run", slow_run)
    response = TestClient(application).post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Tìm laptop"},
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "gateway.orchestration_timeout"
    assert response.headers["X-Trace-ID"] == response.json()["error"]["trace_id"]
    assert cancelled.is_set()


def test_sse_streams_real_progress_and_exactly_one_terminal_event() -> None:
    application = create_app(build_test_settings())

    with TestClient(application).stream(
        "POST",
        "/api/v1/chat/stream",
        headers=ALICE_HEADERS,
        json={
            "message": ("Tìm tai nghe dưới 1 triệu, bán tốt và ít bị khách phàn nàn.")
        },
    ) as response:
        response.read()
        stream_text = response.text

    events = parse_sse(stream_text)
    event_names = [event["event"] for event in events]
    status_payloads = [event["data"] for event in events if event["event"] == "status"]

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert event_names[-1] == "completed"
    assert sum(name in {"completed", "error"} for name in event_names) == 1
    assert {payload["phase"] for payload in status_payloads} >= {
        "request.accepted",
        "routing.completed",
        "planning.completed",
        "agent.started",
        "agent.completed",
        "aggregation.completed",
    }
    sequences = [payload["sequence"] for payload in status_payloads]
    assert sequences == sorted(sequences)
    completed = events[-1]["data"]
    assert completed["status"] == "success"
    assert completed["request_id"] == response.headers["X-Request-ID"]
    assert completed["trace_id"] == response.headers["X-Trace-ID"]
    assert "agent_results" not in stream_text


def test_sse_post_header_exception_is_one_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(build_test_settings())
    runtime = application.state.gateway_runtime

    async def explode(**_: Any) -> OrchestrationResult:
        raise RuntimeError("PRIVATE-EXCEPTION-TEXT")

    monkeypatch.setattr(runtime.orchestrator, "run", explode)
    response = TestClient(application).post(
        "/api/v1/chat/stream",
        headers=ALICE_HEADERS,
        json={"message": "Tìm laptop"},
    )
    events = parse_sse(response.text)

    assert response.status_code == 200
    assert [event["event"] for event in events] == ["status", "error"]
    assert events[-1]["data"]["error"]["code"] == "gateway.internal_error"
    assert "PRIVATE-EXCEPTION-TEXT" not in response.text


def test_gateway_logs_only_safe_request_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = create_app(build_test_settings())
    private_message = "PRIVATE-MESSAGE-928173"

    with caplog.at_level(logging.INFO):
        response = TestClient(application).post(
            "/api/v1/chat",
            headers=ALICE_HEADERS,
            json={"message": private_message},
        )

    assert response.status_code == 200
    assert private_message not in caplog.text
    assert ALICE_HEADERS["X-API-Key"] not in caplog.text
    assert "request_id=" in caplog.text
    assert "trace_id=" in caplog.text


def test_readiness_and_metrics_use_runtime_adapters() -> None:
    application = create_app(build_test_settings())
    client = TestClient(application)

    ready = client.get("/readyz")
    client.post(
        "/api/v1/chat",
        headers=ALICE_HEADERS,
        json={"message": "Xin chào"},
    )
    metrics = client.get("/metrics")

    assert ready.status_code == 200
    assert ready.json()["checks"] == {"runtime": "ok", "database": "ok"}
    assert metrics.status_code == 200
    assert "http_requests_total" in metrics.text
    assert "agent_operations_total" in metrics.text


def test_settings_reject_insecure_production_gateway() -> None:
    with pytest.raises(ValueError, match="non-default"):
        Settings(
            _env_file=None,
            app_env="production",
            legacy_chat_enabled=False,
        )
    with pytest.raises(ValueError, match="LEGACY_CHAT_ENABLED"):
        Settings(
            _env_file=None,
            app_env="production",
            gateway_api_keys="production:strong-production-key",
            legacy_chat_enabled=True,
        )

    production = Settings(
        _env_file=None,
        app_env="production",
        gateway_api_keys="production:strong-production-key",
        legacy_chat_enabled=False,
        shared_state_backend="redis",
        knowledge_backend="qdrant",
        qdrant_api_key="strong-qdrant-key",
        operations_api_key="strong-operations-key",
    )
    assert production.app_env == "production"
    production_app = create_app(production)
    assert (
        TestClient(production_app)
        .post(
            "/chat",
            json={"message": "legacy must be disabled"},
        )
        .status_code
        == 404
    )


def build_test_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "gateway_api_keys": ("alice:alice-secret-key,bob:bob-secret-key"),
        "gateway_rate_limit_requests": 10,
        "gateway_rate_limit_window_seconds": 60,
        "legacy_chat_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def parse_sse(value: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in value.replace("\r\n", "\n").split("\n\n"):
        if not block or block.startswith(":"):
            continue
        parsed: dict[str, Any] = {}
        for line in block.splitlines():
            field, _, content = line.partition(":")
            content = content.lstrip()
            if field == "data":
                parsed[field] = json.loads(content)
            elif field in {"event", "id", "retry"}:
                parsed[field] = content
        events.append(parsed)
    return events
