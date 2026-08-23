"""Public service health and metrics endpoints."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.db import session as db_session
from app.gateway.dependencies import get_runtime
from app.gateway.errors import GatewayAPIError

router = APIRouter(tags=["operations"])


@router.get("/health")
@router.get("/livez")
async def live() -> dict[str, str]:
    """Process liveness does not depend on downstream availability."""

    return {"status": "ok"}


@router.get("/readyz")
async def ready(request: Request) -> JSONResponse:
    """Readiness verifies runtime composition and a bounded database round-trip."""

    runtime = get_runtime(request)
    checks = {"runtime": "ok"}
    try:
        async with asyncio.timeout(2):
            await asyncio.to_thread(_database_ping)
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "failed"

    if runtime.redis_client is not None:
        try:
            async with asyncio.timeout(2):
                await asyncio.to_thread(runtime.redis_client.ping)
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "failed"

    if runtime.knowledge_store is not None:
        try:
            async with asyncio.timeout(2):
                await asyncio.to_thread(runtime.knowledge_store.ready)
            checks["qdrant"] = "ok"
        except Exception:
            checks["qdrant"] = "failed"

    if "failed" in checks.values():
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "checks": checks,
            },
        )
    return JSONResponse(
        content={
            "status": "ready",
            "checks": checks,
        }
    )


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    """Expose dependency-light Prometheus text without request or user content."""

    runtime = get_runtime(request)
    _authorize_operations(request)
    return PlainTextResponse(
        runtime.telemetry.metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4",
    )


@router.get("/api/v1/operations/traces/{trace_id}")
async def trace_details(trace_id: str, request: Request) -> dict[str, object]:
    """Return bounded, redacted trace events for an authorized operator."""

    runtime = get_runtime(request)
    _authorize_operations(request)
    events = runtime.telemetry.events(trace_id=trace_id)
    if not events:
        raise GatewayAPIError(
            status_code=404,
            code="gateway.trace_not_found",
            message="Không tìm thấy trace trong cửa sổ lưu trữ hiện tại.",
        )
    return {
        "trace_id": trace_id,
        "count": len(events),
        "events": [event.model_dump(mode="json") for event in events],
    }


@router.get("/api/v1/operations/audit")
async def gateway_audit(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """Expose only the Agent Gateway's redacted audit metadata."""

    runtime = get_runtime(request)
    _authorize_operations(request)
    records = runtime.agent_gateway.audit_records()[-limit:]
    return {
        "count": len(records),
        "records": [record.model_dump(mode="json") for record in records],
    }


@router.get("/api/v1/operations/agents")
async def agent_inventory(request: Request) -> dict[str, object]:
    """Expose the validated registry inventory without credentials."""

    runtime = get_runtime(request)
    _authorize_operations(request)
    bundles = runtime.agent_gateway.registry.list()
    return {
        "count": len(bundles),
        "agents": [bundle.model_dump(mode="json") for bundle in bundles],
    }


def _database_ping() -> None:
    with db_session.SessionLocal() as session:
        session.execute(text("SELECT 1"))


def _authorize_operations(request: Request) -> None:
    runtime = get_runtime(request)
    expected = runtime.config.operations_api_key.get_secret_value()
    if not expected and runtime.config.app_env != "production":
        return
    provided = request.headers.get("X-Operations-Key", "")
    if not provided or not hmac.compare_digest(provided, expected):
        raise GatewayAPIError(
            status_code=401,
            code="gateway.operations_authentication_failed",
            message="Không thể xác thực quyền vận hành.",
        )
