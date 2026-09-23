"""Protected health, metrics, traces, registry, and audit API contracts."""

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent_gateway import GatewayRequest
from app.contracts import AuthorizationContext
from app.core.config import Settings
from app.db import session as db_session
from app.db.migrate import EXPECTED_DATABASE_REVISION
from app.gateway import operations
from app.gateway.operations import _database_ping
from app.main import create_app
from app.shared import ExecutionContext


def test_configured_operations_plane_rejects_missing_or_wrong_key() -> None:
    client = operations_client()

    missing = client.get("/metrics")
    wrong = client.get(
        "/api/v2/operations/agents",
        headers={"X-Operations-Key": "wrong-operations-key"},
    )

    for response in (missing, wrong):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == (
            "gateway.operations_authentication_failed"
        )
        assert response.headers["cache-control"] == "no-store"


def test_operations_plane_accepts_bearer_auth_for_prometheus_scraping() -> None:
    response = operations_client().get(
        "/metrics",
        headers={"Authorization": "Bearer strong-operations-key"},
    )

    assert response.status_code == 200
    assert "strong-operations-key" not in response.text


def test_operations_plane_exposes_only_redacted_execution_metadata() -> None:
    client = operations_client()
    runtime = client.app.state.gateway_runtime
    context = ExecutionContext.create(
        principal_id="test",
        session_id="sess_operations_test",
        authorization=AuthorizationContext(
            principal_id="test",
            tenant_id="default",
            scopes=frozenset({"ecommerce.read"}),
        ),
        request_id="req_operations_test",
        trace_id="trace_operations_test",
    )
    for index in range(4):
        runtime.telemetry.record(
            context,
            component="test",
            operation=f"step_{index}",
            outcome="success",
            duration_ms=0.1,
            attributes={"step": index},
        )
    audit_result = asyncio.run(
        runtime.agent_gateway.execute(
            GatewayRequest(
                agent_id="missing_agent",
                task_id="task_operations_test",
                request_id=context.request_id,
                trace_id=context.trace_id,
                authorization=context.authorization,
                action="catalog.search",
                server_id="server_operations_test",
                tool_name="search_books",
                arguments={"private_query": "Tìm sách dưới 150 nghìn"},
            )
        )
    )
    assert audit_result.ok is False
    trace_id = context.trace_id
    operations_headers = {"X-Operations-Key": "strong-operations-key"}

    ready = client.get("/readyz")
    metrics = client.get("/metrics", headers=operations_headers)
    trace = client.get(
        f"/api/v2/operations/traces/{trace_id}",
        headers=operations_headers,
    )
    audit = client.get("/api/v2/operations/audit?limit=20", headers=operations_headers)
    agents = client.get("/api/v2/operations/agents", headers=operations_headers)

    assert ready.status_code == 200
    assert ready.json()["checks"]["knowledge"] == "disabled"
    assert metrics.status_code == 200
    assert "http_requests_total" in metrics.text
    assert "# TYPE http_request_duration_seconds histogram" in metrics.text
    assert "dependency_readiness_checks_total" in metrics.text
    assert metrics.headers["cache-control"] == "no-store"
    assert trace.status_code == 200
    assert trace.json()["trace_id"] == trace_id
    assert trace.json()["count"] >= 4
    assert all("attributes" in event for event in trace.json()["events"])
    assert audit.status_code == 200
    assert audit.json()["count"] >= 1
    assert all("argument_keys" in record for record in audit.json()["records"])
    assert "arguments" not in audit.text
    assert agents.status_code == 200
    assert agents.json()["count"] == 5
    assert "product_agent" in {item["agent_id"] for item in agents.json()["agents"]}
    combined = trace.text + audit.text + agents.text
    assert "test-secret-key" not in combined
    assert "strong-operations-key" not in combined
    assert "Tìm sách" not in combined


def test_unknown_trace_has_stable_not_found_envelope() -> None:
    response = operations_client().get(
        "/api/v2/operations/traces/trace_missing_123",
        headers={"X-Operations-Key": "strong-operations-key"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "gateway.trace_not_found"


def test_database_readiness_rejects_stale_production_revision() -> None:
    with db_session.SessionLocal.begin() as session:
        session.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('stale')")
        )

    with pytest.raises(RuntimeError, match="expected revision"):
        _database_ping(require_current_revision=True)

    with db_session.SessionLocal.begin() as session:
        session.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": EXPECTED_DATABASE_REVISION},
        )

    _database_ping(require_current_revision=True)


def test_production_readiness_enables_revision_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            gateway_api_keys="test:test-secret-key",
        )
    )
    runtime = application.state.gateway_runtime
    application.state.gateway_runtime = replace(
        runtime,
        config=runtime.config.model_copy(update={"app_env": "production"}),
    )
    revision_requirements: list[bool] = []

    def record_database_check(*, require_current_revision: bool = False) -> None:
        revision_requirements.append(require_current_revision)

    monkeypatch.setattr(operations, "_database_ping", record_database_check)

    response = TestClient(application).get("/readyz")

    assert response.status_code == 200
    assert revision_requirements == [True]


def operations_client() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                _env_file=None,
                app_env="test",
                gateway_api_keys="test:test-secret-key",
                gateway_principal_policies="test:default:ecommerce.read",
                operations_api_key="strong-operations-key",
            )
        )
    )
