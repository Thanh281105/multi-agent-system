"""End-to-end hybrid orchestration with a schema-aware fake OpenAI client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent_gateway import AgentGateway
from app.agents import build_default_dispatcher
from app.agents.reasoning import SpecialistInsight
from app.gateway.schemas import build_chat_response
from app.mcp.catalog import build_default_mcp_router
from app.orchestrator import MultiAgentOrchestrator
from app.orchestrator.aggregator import ResultAggregator
from app.orchestrator.model_schemas import (
    GroundedClaim,
    GroundedSynthesis,
    PlanningDecision,
    RoutingDecision,
    RoutingEntities,
)
from app.orchestrator.planner import ExecutionPlanner
from app.orchestrator.router import IntentRouter
from app.shared import ModelRuntimeMode, OpenAIModelRuntime


class SchemaAwareResponses:
    def __init__(
        self,
        *,
        invalid_plan: bool = False,
        invalid_intent: bool = False,
        invalid_entities: bool = False,
        invalid_specialist: bool = False,
        invalid_synthesis: bool = False,
        fail_schema: type[Any] | None = None,
    ) -> None:
        self.invalid_plan = invalid_plan
        self.invalid_intent = invalid_intent
        self.invalid_entities = invalid_entities
        self.invalid_specialist = invalid_specialist
        self.invalid_synthesis = invalid_synthesis
        self.fail_schema = fail_schema
        self.requests: list[dict[str, Any]] = []

    async def parse(self, **request: Any) -> Any:
        self.requests.append(request)
        schema = request["text_format"]
        is_routing_schema = isinstance(schema, type) and issubclass(
            schema, RoutingDecision
        )
        if schema is self.fail_schema or (
            self.fail_schema is RoutingDecision and is_routing_schema
        ):
            raise TimeoutError("forced model stage outage")
        if is_routing_schema:
            routing_payload = {
                "intent": (
                    "product.search" if self.invalid_intent else "multi.recommendation"
                ),
                "confidence": 0.98,
                "entities": (
                    RoutingEntities(product_id=987_654_321)
                    if self.invalid_entities
                    else RoutingEntities(
                        max_price=150_000,
                    )
                ),
                "rationale": "recommendation_with_complaint_constraint",
            }
            value: Any = (
                schema.model_construct(**routing_payload)
                if self.invalid_intent
                else schema(**routing_payload)
            )
        elif schema is PlanningDecision:
            value = PlanningDecision(
                capabilities=(
                    ["market.analyze"]
                    if self.invalid_plan
                    else ["product.rank", "review.compare", "trust.compare"]
                ),
                candidate_limit=5,
                rationale="rank_then_parallel_review_and_trust",
            )
        elif schema is SpecialistInsight:
            payload = json.loads(request["input"])
            fact_ids = [item["fact_id"] for item in payload["fact_catalog"]]
            value = SpecialistInsight(
                selected_fact_ids=(
                    ["fact_999"] if self.invalid_specialist else fact_ids[:2]
                ),
                confidence=0.9,
            )
        elif schema is GroundedSynthesis:
            payload = json.loads(request["input"])
            claim_ids = [item["claim_id"] for item in payload["claim_catalog"]]
            value = GroundedSynthesis(
                claims=[
                    GroundedClaim(claim_id=claim_id)
                    for claim_id in (
                        ["claim_999"] if self.invalid_synthesis else claim_ids
                    )
                ]
            )
        else:  # pragma: no cover - catches accidental schema expansion
            raise AssertionError(f"unexpected schema: {schema}")
        return SimpleNamespace(
            status="completed",
            output_parsed=value,
            usage=SimpleNamespace(input_tokens=20, output_tokens=10, total_tokens=30),
        )


class FakeClient:
    def __init__(
        self,
        *,
        invalid_plan: bool = False,
        invalid_intent: bool = False,
        invalid_entities: bool = False,
        invalid_specialist: bool = False,
        invalid_synthesis: bool = False,
        fail_schema: type[Any] | None = None,
    ) -> None:
        self.responses = SchemaAwareResponses(
            invalid_plan=invalid_plan,
            invalid_intent=invalid_intent,
            invalid_entities=invalid_entities,
            invalid_specialist=invalid_specialist,
            invalid_synthesis=invalid_synthesis,
            fail_schema=fail_schema,
        )


def build_hybrid_orchestrator(
    *,
    runtime_mode: ModelRuntimeMode = "hybrid",
    invalid_plan: bool = False,
    invalid_intent: bool = False,
    invalid_entities: bool = False,
    invalid_specialist: bool = False,
    invalid_synthesis: bool = False,
    fail_schema: type[Any] | None = None,
) -> tuple[MultiAgentOrchestrator, SchemaAwareResponses]:
    client = FakeClient(
        invalid_plan=invalid_plan,
        invalid_intent=invalid_intent,
        invalid_entities=invalid_entities,
        invalid_specialist=invalid_specialist,
        invalid_synthesis=invalid_synthesis,
        fail_schema=fail_schema,
    )
    runtime = OpenAIModelRuntime(
        "test-key",
        client=client,
        max_retries=0,
    )
    gateway = AgentGateway(router=build_default_mcp_router())
    orchestrator = MultiAgentOrchestrator(
        dispatcher=build_default_dispatcher(
            gateway,
            model_runtime=runtime,
            runtime_mode=runtime_mode,
        ),
        router=IntentRouter(
            model_runtime=runtime,
            runtime_mode=runtime_mode,
        ),
        planner=ExecutionPlanner(
            model_runtime=runtime,
            runtime_mode=runtime_mode,
        ),
        aggregator=ResultAggregator(
            model_runtime=runtime,
            runtime_mode=runtime_mode,
        ),
    )
    return orchestrator, client.responses


@pytest.mark.asyncio
async def test_hybrid_flow_runs_all_structured_reasoning_stages() -> None:
    orchestrator, responses = build_hybrid_orchestrator()

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id="sess_hybrid_123",
    )
    public = build_chat_response(result)

    assert result.intent == "multi.recommendation"
    assert [step.agent_id for step in result.plan.steps] == [
        "product_agent",
        "review_agent",
        "trust_agent",
    ]
    assert all("model_insight" in item.data for item in result.agent_results)
    assert [item.stage for item in result.model_calls] == [
        "routing",
        "planning",
        "specialist.analysis",
        "specialist.analysis",
        "specialist.analysis",
        "synthesis",
    ]
    assert all(item.status == "success" for item in result.model_calls)
    assert "nguồn: tiki-books:kaggle-v4:test" in result.answer
    assert "snapshot lịch sử Tiki Books" in result.answer
    assert public.executions[1].depends_on == ("step_product",)
    assert len(public.model_calls) == 6
    assert all(request["store"] is False for request in responses.requests)
    assert "instructions" not in public.model_dump_json()

    routing_request = next(
        request
        for request in responses.requests
        if issubclass(request["text_format"], RoutingDecision)
    )
    routing_input = json.loads(routing_request["input"])
    assert routing_input["authorized_intent"] == "multi.recommendation"
    assert routing_input["authorized_entities"] == {"max_price": 150_000}
    assert (
        routing_request["text_format"].model_json_schema()["properties"]["intent"][
            "const"
        ]
        == "multi.recommendation"
    )

    planning_request = next(
        request
        for request in responses.requests
        if request["text_format"] is PlanningDecision
    )
    planning_input = json.loads(planning_request["input"])
    assert planning_input["authorized_capability_sequence"] == [
        "product.rank",
        "review.compare",
        "trust.compare",
    ]


@pytest.mark.asyncio
async def test_required_mode_accepts_exact_authorized_model_contract() -> None:
    orchestrator, _ = build_hybrid_orchestrator(runtime_mode="required")

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id="sess_required_authorized_123",
    )

    assert result.status == "success"
    assert [step.action for step in result.plan.steps] == [
        "product.rank",
        "review.compare",
        "trust.compare",
    ]
    assert all(not item.fallback_used for item in result.model_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_overrides", "error"),
    [
        ({"invalid_intent": True}, "routing_intent_not_authorized"),
        ({"invalid_entities": True}, "routing_entities_not_authorized"),
        ({"invalid_plan": True}, "model_plan_not_authorized"),
    ],
)
async def test_required_mode_rejects_model_output_outside_authorized_contract(
    runtime_overrides: dict[str, bool],
    error: str,
) -> None:
    orchestrator, _ = build_hybrid_orchestrator(
        runtime_mode="required",
        **runtime_overrides,
    )

    with pytest.raises(ValueError, match=error):
        await orchestrator.run(
            message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
            principal_id="user-a",
            session_id=f"sess_required_rejected_{error}",
        )


@pytest.mark.asyncio
async def test_unauthorized_model_plan_falls_back_to_compiled_allowlist() -> None:
    orchestrator, _ = build_hybrid_orchestrator(invalid_plan=True)

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id="sess_hybrid_fallback_123",
    )

    assert [step.action for step in result.plan.steps] == [
        "product.rank",
        "review.compare",
        "trust.compare",
    ]
    planning_call = next(
        item for item in result.model_calls if item.stage == "planning"
    )
    assert planning_call.fallback_used is True
    assert planning_call.fallback_reason == "unauthorized_plan"


@pytest.mark.asyncio
async def test_unauthorized_model_intent_falls_back_to_deterministic_route() -> None:
    orchestrator, _ = build_hybrid_orchestrator(invalid_intent=True)

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id="sess_hybrid_intent_fallback_123",
    )

    assert result.intent == "multi.recommendation"
    routing_call = next(item for item in result.model_calls if item.stage == "routing")
    assert routing_call.fallback_used is True
    assert routing_call.fallback_reason == "unauthorized_routing_intent"


@pytest.mark.asyncio
async def test_model_cannot_invent_routing_entities() -> None:
    orchestrator, _ = build_hybrid_orchestrator(invalid_entities=True)

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id="sess_hybrid_bad_entity_123",
    )

    routing_call = next(item for item in result.model_calls if item.stage == "routing")
    assert routing_call.fallback_used is True
    assert routing_call.fallback_reason == "ungrounded_routing_entities"
    assert "987654321" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_model_cannot_select_unknown_specialist_fact() -> None:
    orchestrator, _ = build_hybrid_orchestrator(invalid_specialist=True)

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id="sess_hybrid_bad_fact_123",
    )

    specialist_calls = [
        item for item in result.model_calls if item.stage == "specialist.analysis"
    ]
    assert specialist_calls
    assert all(
        item.fallback_reason == "ungrounded_specialist" for item in specialist_calls
    )
    assert all("model_insight" not in item.data for item in result.agent_results)


@pytest.mark.asyncio
async def test_model_cannot_select_unknown_synthesis_claim() -> None:
    orchestrator, _ = build_hybrid_orchestrator(invalid_synthesis=True)

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id="sess_hybrid_bad_claim_123",
    )

    synthesis_call = next(
        item for item in result.model_calls if item.stage == "synthesis"
    )
    assert synthesis_call.fallback_used is True
    assert synthesis_call.fallback_reason == "ungrounded_synthesis"
    assert "claim_999" not in result.answer
    assert "(nguồn:" not in result.answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "stage", "fallback_reason"),
    [
        (RoutingDecision, "routing", "deterministic_routing"),
        (PlanningDecision, "planning", "deterministic_planning"),
        (SpecialistInsight, "specialist.analysis", "deterministic_agent_result"),
        (GroundedSynthesis, "synthesis", "deterministic_synthesis"),
    ],
)
async def test_model_stage_outage_preserves_deterministic_book_fallback(
    schema: type[Any],
    stage: str,
    fallback_reason: str,
) -> None:
    orchestrator, _ = build_hybrid_orchestrator(fail_schema=schema)

    result = await orchestrator.run(
        message="Tìm sách dưới 150 nghìn, đáng mua và ít bị phàn nàn.",
        principal_id="user-a",
        session_id=f"sess_hybrid_outage_{stage.replace('.', '_')}",
    )

    matching_calls = [item for item in result.model_calls if item.stage == stage]
    assert matching_calls
    assert all(item.fallback_used for item in matching_calls)
    assert all(item.fallback_reason == fallback_reason for item in matching_calls)
    assert result.intent == "multi.recommendation"
    assert result.selected_product_id is not None
    assert "snapshot lịch sử Tiki Books" in result.answer


@pytest.mark.asyncio
async def test_hybrid_model_cannot_promote_non_book_request() -> None:
    orchestrator, responses = build_hybrid_orchestrator()

    result = await orchestrator.run(
        message="Tìm tai nghe dưới 1 triệu",
        principal_id="user-a",
        session_id="sess_hybrid_non_book",
    )

    assert result.intent == "general.unsupported"
    assert result.plan.steps == ()
    assert result.model_calls == ()
    assert responses.requests == []


def test_grounded_claim_schema_rejects_model_authored_prose() -> None:
    with pytest.raises(ValidationError):
        GroundedClaim.model_validate(
            {
                "claim_id": "claim_001",
                "statement": "unchecked model prose",
            }
        )
