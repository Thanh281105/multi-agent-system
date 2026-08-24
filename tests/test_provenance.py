"""End-to-end provenance checks for imported public data."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_gateway import AgentGateway
from app.agents import build_default_dispatcher
from app.contracts import AgentMessage, TaskStatus
from app.db.public_import import import_public_snapshot
from app.mcp.catalog import build_default_mcp_router

SNAPSHOT_DIR = (
    Path(__file__).resolve().parents[1] / "data" / "snapshots" / "tiki-books-v4-sample"
)


@pytest.mark.asyncio
async def test_public_snapshot_provenance_reaches_product_agent() -> None:
    import_public_snapshot(snapshot_dir=SNAPSHOT_DIR)
    dispatcher = build_default_dispatcher(
        AgentGateway(router=build_default_mcp_router())
    )

    result = await dispatcher.dispatch(
        AgentMessage(
            task_id="task_public_provenance",
            session_id="sess_public_provenance",
            request_id="req_public_provenance",
            trace_id="trace_public_provenance",
            source="orchestrator",
            target="product_agent",
            action="product.search",
            payload={"category": "Đạo đức - Kỹ năng sống"},
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["count"] >= 1
    assert result.provenance
    assert result.provenance[0].source_type == "sample.public_dataset"
    assert result.provenance[0].source_id == "tiki-books:kaggle-v4"
