"""Contract, registry, MCP, and outbound gateway regression tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.agent_gateway import (
    AgentGateway,
    GatewayRequest,
    SlidingWindowRateLimiter,
)
from app.contracts import AgentMessage, ExecutionPlan, ExecutionStep
from app.mcp import MCPRouter, MCPToolSpec
from app.registry import AgentBundle, AgentRegistry, RateLimitPolicy, default_registry


def gateway_request(**overrides: Any) -> GatewayRequest:
    values: dict[str, Any] = {
        "agent_id": "product_agent",
        "task_id": "task_platform",
        "request_id": "req_platform",
        "trace_id": "trace_platform",
        "server_id": "product_db",
        "tool_name": "search_products",
        "arguments": {"query": "tai nghe"},
    }
    values.update(overrides)
    return GatewayRequest(**values)


def test_a2a_message_uses_workflow_input_alias_and_forbids_extra_fields() -> None:
    message = AgentMessage.model_validate(
        {
            "task_id": "task_contract",
            "session_id": "sess_contract",
            "request_id": "req_contract",
            "trace_id": "trace_contract",
            "source": "orchestrator",
            "target": "product_agent",
            "action": "product.search",
            "input": {"category": "Tai nghe"},
        }
    )

    assert message.payload == {"category": "Tai nghe"}
    assert message.model_dump(by_alias=True)["input"] == {"category": "Tai nghe"}
    with pytest.raises(ValidationError):
        AgentMessage.model_validate(
            {
                **message.model_dump(by_alias=True),
                "unexpected": True,
            }
        )


def test_execution_plan_rejects_forward_or_missing_dependencies() -> None:
    with pytest.raises(ValidationError, match="unresolved dependencies"):
        ExecutionPlan(
            plan_id="plan_invalid",
            intent="review.summary",
            steps=(
                ExecutionStep(
                    step_id="step_review",
                    agent_id="review_agent",
                    action="review.summarize",
                    depends_on=("step_product",),
                ),
            ),
        )


def test_default_registry_exposes_all_workflow_agents() -> None:
    assert {bundle.agent_id for bundle in default_registry.list()} == {
        "orchestrator",
        "product_agent",
        "review_agent",
        "trust_agent",
        "market_agent",
    }
    assert default_registry.find_by_capability("review.complaint")[0].agent_id == (
        "trust_agent"
    )


@pytest.mark.asyncio
async def test_gateway_forwards_only_allowlisted_tool_and_redacts_values() -> None:
    router = MCPRouter()
    router.register(
        MCPToolSpec(
            server_id="product_db",
            tool_name="search_products",
            skill_id="search_products",
            required_permission="product.read",
            description="Search sample products.",
            handler=lambda **arguments: {"count": 1, "query": arguments["query"]},
        )
    )
    gateway = AgentGateway(router=router)

    response = await gateway.execute(gateway_request())

    assert response.ok is True
    assert response.data == {"count": 1, "query": "tai nghe"}
    audit = gateway.audit_records()[0]
    assert audit.argument_keys == ("query",)
    assert "tai nghe" not in audit.model_dump_json()


@pytest.mark.asyncio
async def test_agent_gateway_denies_skill_outside_bundle() -> None:
    router = MCPRouter()
    router.register(
        MCPToolSpec(
            server_id="product_db",
            tool_name="delete_products",
            skill_id="delete_products",
            required_permission="product.write",
            description="A tool that no agent should receive.",
            handler=lambda: {"deleted": True},
        )
    )
    gateway = AgentGateway(router=router)

    response = await gateway.execute(
        gateway_request(tool_name="delete_products", arguments={})
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "gateway.skill_forbidden"


@pytest.mark.asyncio
async def test_agent_gateway_enforces_atomic_per_agent_rate_limit() -> None:
    now = [100.0]
    bundle = AgentBundle(
        agent_id="limited_agent",
        version="1.0.0",
        description="Test-only limited agent.",
        capabilities=("product.search",),
        skills=("search_products",),
        permissions=frozenset({"product.read"}),
        mcp_servers=frozenset({"product_db"}),
        rate_limit=RateLimitPolicy(requests=1, window_seconds=60),
    )
    registry = AgentRegistry((bundle,))
    router = MCPRouter()
    router.register(
        MCPToolSpec(
            server_id="product_db",
            tool_name="search_products",
            skill_id="search_products",
            required_permission="product.read",
            description="Search sample products.",
            handler=lambda: {"count": 0},
        )
    )
    gateway = AgentGateway(
        router=router,
        registry=registry,
        limiter=SlidingWindowRateLimiter(clock=lambda: now[0]),
    )
    request = gateway_request(
        agent_id="limited_agent",
        arguments={},
    )

    first = await gateway.execute(request)
    second = await gateway.execute(request)
    now[0] += 61
    third = await gateway.execute(request)

    assert first.ok is True
    assert second.ok is False
    assert second.error is not None
    assert second.error.code == "gateway.rate_limited"
    assert second.error.retryable is True
    assert third.ok is True
