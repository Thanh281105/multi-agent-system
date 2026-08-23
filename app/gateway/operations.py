"""Public service health and metrics endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.db import session as db_session
from app.gateway.dependencies import get_runtime

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
    return PlainTextResponse(
        runtime.telemetry.metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4",
    )


def _database_ping() -> None:
    with db_session.SessionLocal() as session:
        session.execute(text("SELECT 1"))
