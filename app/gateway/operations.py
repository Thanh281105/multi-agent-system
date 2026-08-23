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

    get_runtime(request)
    try:
        async with asyncio.timeout(2):
            await asyncio.to_thread(_database_ping)
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "checks": {"runtime": "ok", "database": "failed"},
            },
        )
    return JSONResponse(
        content={
            "status": "ready",
            "checks": {"runtime": "ok", "database": "ok"},
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
