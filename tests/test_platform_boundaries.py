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
from app.agents.skill_manifest import SkillManifest, SkillRegistry
from app.contracts import (
    AgentMessage,
    AgentResult,
    AuthorizationContext,
    ExecutionPlan,
    ExecutionStep,
    TaskStatus,
)
from app.core.config import Settings
from app.mcp import MCPRouter, MCPToolSpec
from app.orchestrator.executor import PlanExecutor
from app.orchestrator.planner import ExecutionPlanner
from app.orchestrator.schemas import RoutedIntent
from app.registry import (
    AgentBundle,
    AgentRegistry,
    IntentManifest,
    RateLimitPolicy,
    default_registry,
)
from app.shared import ExecutionContext, Telemetry


def gateway_request(**overrides: Any) -> GatewayRequest:
    values: dict[str, Any] = {
        "agent_id": "product_agent",
        "task_id": "task_platform",
        "request_id": "req_platform",
        "trace_id": "trace_platform",
        "authorization": {
            "principal_id": "test-principal",
            "scopes": ["ecommerce.read"],
        },
        "action": "product.search",
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
            "authorization": {"principal_id": "test-principal"},
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
    with pytest.raises(ValidationError, match="authorization"):
        AgentMessage.model_validate(
            {
                "task_id": "task_without_auth",
                "session_id": "sess_without_auth",
                "request_id": "req_without_auth",
                "trace_id": "trace_without_auth",
                "source": "orchestrator",
                "target": "product_agent",
                "action": "product.search",
            }
        )


def test_legacy_http_path_is_disabled_by_default() -> None:
    assert Settings(_env_file=None, app_env="test").legacy_chat_enabled is False


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
    assert default_registry.active_agent_for_intent("review.summary") == "review_agent"
    assert default_registry.follow_up_intent("market_agent") == "market.analyze"
    assert default_registry.intent_manifest("multi.recommendation") is not None


def test_planner_compiles_a_registered_domain_without_core_branch() -> None:
    def build_steps(_: dict[str, Any]) -> tuple[ExecutionStep, ...]:
        return (
            ExecutionStep(
                step_id="step_support",
                agent_id="support_agent",
                action="support.answer",
            ),
        )

    registry = AgentRegistry(
        (
            AgentBundle(
                agent_id="support_agent",
                version="1.0.0",
                description="Answer support questions.",
                capabilities=("support.answer",),
            ),
        ),
        intent_manifests=(
            IntentManifest(
                intent="support.answer",
                active_agent_id="support_agent",
                build_steps=build_steps,
                expected_capabilities=lambda _: ("support.answer",),
            ),
        ),
    )

    plan = ExecutionPlanner(registry=registry).build(
        RoutedIntent(
            intent="support.answer",
            confidence=1.0,
            routing_rule="test_manifest",
        )
    )

    assert plan.intent == "support.answer"
    assert plan.steps[0].agent_id == "support_agent"
    assert registry.active_agent_for_intent(plan.intent) == "support_agent"


@pytest.mark.asyncio
async def test_plan_executor_uses_the_injected_registry_for_custom_agents() -> None:
    bundle = AgentBundle(
        agent_id="support_agent",
        version="2.3.4",
        description="Test-only support agent.",
        capabilities=("support.answer",),
    )
    registry = AgentRegistry((bundle,))

    class Dispatcher:
        async def dispatch(self, message: AgentMessage) -> AgentResult:
            return AgentResult(
                task_id=message.task_id,
                agent_id=message.target,
                status=TaskStatus.SUCCESS,
            )

    executor = PlanExecutor(
        Dispatcher(),
        Telemetry(),
        registry=registry,
    )  # type: ignore[arg-type]
    context = ExecutionContext.create(
        principal_id="test-principal",
        session_id="sess_executor_123",
    )
    plan = ExecutionPlan(
        plan_id="plan_executor_123",
        intent="support.answer",
        steps=(
            ExecutionStep(
                step_id="step_support",
                agent_id="support_agent",
                action="support.answer",
            ),
        ),
    )

    results = await executor.execute(plan, context)

    assert results[0].status == TaskStatus.SUCCESS


def test_custom_agent_gateway_requires_matching_skill_registry() -> None:
    registry = AgentRegistry(
        (
            AgentBundle(
                agent_id="support_agent",
                version="1.0.0",
                description="Test-only support agent.",
                capabilities=("support.answer",),
                skills=("support_answer",),
                permissions=frozenset({"support.read"}),
                mcp_servers=frozenset({"support_db"}),
            ),
        )
    )

    with pytest.raises(ValueError, match="requires an explicit SkillRegistry"):
        AgentGateway(router=MCPRouter(), registry=registry)


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
    assert audit.agent_version == "1.0.0"
    assert audit.skill_id == "search_products"
    assert audit.skill_version == "1.0.0"
    assert audit.principal_ref == "test-princip"
    assert audit.tenant_id == "default"
    assert "tai nghe" not in audit.model_dump_json()


@pytest.mark.asyncio
async def test_agent_gateway_denies_a_user_scope_missing_from_the_tool_policy() -> None:
    router = MCPRouter()
    router.register(
        MCPToolSpec(
            server_id="product_db",
            tool_name="search_products",
            skill_id="search_products",
            required_permission="product.read",
            description="Search sample products.",
            handler=lambda: {"count": 0},
            required_user_scope="ecommerce.read",
        )
    )
    response = await AgentGateway(router=router).execute(
        gateway_request(authorization={"principal_id": "test-principal", "scopes": []})
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "gateway.user_scope_denied"


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
        skill_registry=SkillRegistry(
            (
                SkillManifest(
                    agent_id="limited_agent",
                    skill_id="search_products",
                    version="1.0.0",
                    actions=frozenset({"product.search"}),
                    server_id="product_db",
                    tool_name="search_products",
                    instructions_ref="skills/limited_agent/search_products@1.0.0",
                ),
            )
        ),
        limiter=SlidingWindowRateLimiter(clock=lambda: now[0]),
    )
    request = gateway_request(
        agent_id="limited_agent",
        authorization=AuthorizationContext(
            principal_id="test-principal",
            scopes=frozenset({"ecommerce.read"}),
        ),
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
