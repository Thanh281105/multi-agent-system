"""End-to-end domain agent tests through Agent Gateway and MCP boundaries."""

from __future__ import annotations

import pytest

from app.agent_gateway import AgentGateway
from app.agents import AgentDispatcher, build_default_dispatcher
from app.contracts import AgentMessage, TaskStatus
from app.mcp.catalog import build_default_mcp_router


def message(
    *,
    target: str,
    action: str,
    payload: dict[str, object],
) -> AgentMessage:
    return AgentMessage(
        task_id=f"task_{target}",
        session_id="sess_domain_123",
        request_id="req_domain_123",
        trace_id="trace_domain_123",
        source="orchestrator",
        target=target,
        action=action,
        payload=payload,
    )


@pytest.fixture
def runtime() -> tuple[AgentDispatcher, AgentGateway]:
    gateway = AgentGateway(router=build_default_mcp_router())
    return build_default_dispatcher(gateway), gateway


@pytest.mark.asyncio
async def test_product_agent_searches_and_ranks_sample_facts(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, gateway = runtime

    result = await dispatcher.dispatch(
        message(
            target="product_agent",
            action="product.rank",
            payload={
                "category": "Tai nghe",
                "max_price": 1_000_000,
                "min_rating": 4.5,
            },
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["search"]["count"] == 2
    assert result.data["ranking"]["count"] == 2
    assert result.data["ranking"]["products"][0]["ranking_score"] >= 0
    assert {record.tool_name for record in gateway.audit_records()} == {
        "search_products",
        "rank_products",
    }


@pytest.mark.asyncio
async def test_review_agent_returns_sentiment_aspects_and_grounded_summary(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, _ = runtime

    result = await dispatcher.dispatch(
        message(
            target="review_agent",
            action="review.summarize",
            payload={"product_id": 1},
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["retrieval"]["count"] == 5
    assert result.data["sentiment"]["count"] == 5
    assert "pin" in result.data["aspects"]["negative_aspects"]
    assert "review" in result.data["summary"]
    assert all(provenance.sample_data for provenance in result.provenance)


@pytest.mark.asyncio
async def test_trust_agent_detects_complaints_without_claiming_model_accuracy(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, _ = runtime

    result = await dispatcher.dispatch(
        message(
            target="trust_agent",
            action="trust.analyze",
            payload={"product_id": 1},
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["trust"]["method"].endswith("rules_v1")
    assert result.data["complaints"]["complaint_count"] >= 1
    assert 0 <= result.data["complaints"]["complaint_rate"] <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "action", "method"),
    [
        ("review_agent", "review.compare", "sentiment_aspects"),
        ("trust_agent", "trust.compare", "trust_complaints"),
    ],
)
async def test_review_intelligence_agents_compare_all_candidate_products(
    runtime: tuple[AgentDispatcher, AgentGateway],
    target: str,
    action: str,
    method: str,
) -> None:
    dispatcher, _ = runtime

    result = await dispatcher.dispatch(
        message(
            target=target,
            action=action,
            payload={"product_ids": [1, 2, 4]},
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["requested_product_ids"] == [1, 2, 4]
    assert [item["product_id"] for item in result.data["analyses"]] == [1, 2, 4]
    assert method in result.data["method"]
    assert all(item["retrieval"]["found"] for item in result.data["analyses"])


@pytest.mark.asyncio
async def test_market_agent_labels_all_evidence_as_sample_data(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, _ = runtime

    result = await dispatcher.dispatch(
        message(
            target="market_agent",
            action="market.analyze",
            payload={"category": "Tai nghe", "query": "tai nghe giá pin"},
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["market"]["representative_of_real_market"] is False
    assert result.data["market"]["statistics"]["product_count"] == 5
    assert all(item["sample_data"] for item in result.data["knowledge"]["documents"])
    assert all(provenance.sample_data for provenance in result.provenance)


@pytest.mark.asyncio
async def test_dispatcher_returns_stable_error_for_unknown_agent(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, _ = runtime

    result = await dispatcher.dispatch(
        message(
            target="unknown_agent",
            action="product.search",
            payload={},
        )
    )

    assert result.status == TaskStatus.FAILED
    assert result.errors[0].code == "dispatcher.agent_not_found"
