"""Multi-agent routing, planning, execution, grounding, and session tests."""

from __future__ import annotations

import pytest

from app.agent_gateway import AgentGateway
from app.agents import build_default_dispatcher
from app.contracts import AgentError, AgentMessage, AgentResult, TaskStatus
from app.mcp.catalog import build_default_mcp_router
from app.orchestrator import MultiAgentOrchestrator
from app.orchestrator.planner import ExecutionPlanner
from app.orchestrator.router import IntentRouter
from app.orchestrator.scoring import score_recommendation_candidates
from app.shared import InMemorySessionStore, SessionOwnershipError


def build_orchestrator() -> tuple[MultiAgentOrchestrator, AgentGateway]:
    gateway = AgentGateway(router=build_default_mcp_router())
    orchestrator = MultiAgentOrchestrator(
        dispatcher=build_default_dispatcher(gateway),
    )
    return orchestrator, gateway


def test_router_and_planner_build_multi_agent_dependency_graph() -> None:
    sessions = InMemorySessionStore()
    session = sessions.create(owner_id="user-a", session_id="sess_plan_123")
    routed = IntentRouter().route(
        "Tìm tai nghe dưới 1 triệu, bán tốt và ít bị khách phàn nàn.",
        session,
    )
    plan = ExecutionPlanner().build(routed)

    assert routed.intent == "multi.recommendation"
    assert routed.entities["category"] == "Tai nghe"
    assert routed.entities["max_price"] == 1_000_000
    assert [step.agent_id for step in plan.steps] == [
        "product_agent",
        "review_agent",
        "trust_agent",
    ]
    assert plan.steps[1].depends_on == ("step_product",)
    assert plan.steps[2].depends_on == ("step_product",)
    assert plan.steps[1].action == "review.compare"
    assert plan.steps[2].action == "trust.compare"
    assert plan.steps[1].input == {"product_ids_all_from": "step_product"}

    formatted_price = IntentRouter().route(
        "Tìm tai nghe dưới 1.000.000, rating ít nhất 4.5",
        session,
    )
    assert formatted_price.entities["max_price"] == 1_000_000
    assert formatted_price.entities["min_rating"] == 4.5

    malformed_price = IntentRouter().route("Tìm tai nghe dưới 1..2", session)
    assert "max_price" not in malformed_price.entities


@pytest.mark.asyncio
async def test_orchestrator_executes_grounded_multi_agent_recommendation() -> None:
    orchestrator, gateway = build_orchestrator()

    result = await orchestrator.run(
        message="Tìm tai nghe dưới 1 triệu, bán tốt và ít bị khách phàn nàn.",
        principal_id="user-a",
        session_id="sess_multi_123",
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.intent == "multi.recommendation"
    assert [item.agent_id for item in result.agent_results] == [
        "product_agent",
        "review_agent",
        "trust_agent",
    ]
    assert "dữ liệu mẫu" in result.answer
    assert "complaint" in result.answer
    assert "Đã so sánh 4 ứng viên" in result.answer
    assert "điểm đa agent" in result.answer
    assert result.active_agent == "product_agent"
    ranked_ids = [
        product["id"]
        for product in result.agent_results[0].data["ranking"]["products"]
    ]
    review_ids = [
        item["product_id"]
        for item in result.agent_results[1].data["analyses"]
    ]
    trust_ids = [
        item["product_id"]
        for item in result.agent_results[2].data["analyses"]
    ]
    assert review_ids == ranked_ids
    assert trust_ids == ranked_ids
    assert result.selected_product_id in ranked_ids
    stored = orchestrator.sessions.get(
        owner_id="user-a",
        session_id=result.session_id,
    )
    assert stored.state["last_product_id"] == result.selected_product_id
    assert len(gateway.audit_records()) == 2 + len(ranked_ids) * 6
    assert all(item.sample_data for item in result.provenance)


@pytest.mark.asyncio
async def test_review_query_resolves_product_name_before_review_analysis() -> None:
    orchestrator, _ = build_orchestrator()

    result = await orchestrator.run(
        message="Khách hàng đánh giá thế nào về Tai nghe Bluetooth Nova Air S2?",
        principal_id="user-a",
        session_id="sess_review_flow_123",
    )

    assert result.status == TaskStatus.SUCCESS
    assert [step.agent_id for step in result.plan.steps] == [
        "product_agent",
        "review_agent",
    ]
    assert "Nova Air S2" in result.answer
    assert result.active_agent == "review_agent"


@pytest.mark.asyncio
async def test_compare_resolves_each_name_then_preserves_comparison_order() -> None:
    orchestrator, _ = build_orchestrator()

    result = await orchestrator.run(
        message=(
            "So sánh Nova Air S2 với Sonic G5. "
            "Nếu ưu tiên giá và rating thì nên chọn sản phẩm nào?"
        ),
        principal_id="user-a",
        session_id="sess_compare_flow_123",
    )

    assert result.status == TaskStatus.SUCCESS
    assert [step.action for step in result.plan.steps] == [
        "product.search",
        "product.search",
        "product.compare",
    ]
    assert "Nova Air S2" in result.answer
    assert "Sonic G5" in result.answer


@pytest.mark.asyncio
async def test_missing_product_is_reported_without_inventing_review_facts() -> None:
    orchestrator, _ = build_orchestrator()

    result = await orchestrator.run(
        message="Khách hàng đánh giá thế nào về SuperDragon X999?",
        principal_id="user-a",
        session_id="sess_missing_123",
    )

    assert result.status == TaskStatus.PARTIAL_SUCCESS
    assert "Không tìm thấy sản phẩm" in result.answer
    assert "SuperDragon" not in result.answer
    assert result.agent_results[-1].errors[0].code == (
        "orchestrator.missing_dependency_data"
    )


@pytest.mark.asyncio
async def test_session_is_owner_bound_and_follow_up_uses_agent_pinning() -> None:
    orchestrator, _ = build_orchestrator()
    first = await orchestrator.run(
        message="Tìm tai nghe dưới 1 triệu",
        principal_id="user-a",
        session_id="sess_owner_123",
    )
    stored = orchestrator.sessions.get(
        owner_id="user-a",
        session_id=first.session_id,
    )
    assert first.agent_results[0].data["products"][0]["id"] == 1
    assert stored.state["last_product_id"] == 1
    follow_up = await orchestrator.run(
        message="Còn pin thì sao?",
        principal_id="user-a",
        session_id=first.session_id,
    )

    assert follow_up.intent == "product.follow_up"
    assert follow_up.plan.steps[0].action == "product.compare"
    assert follow_up.plan.steps[0].input == {"product_ids": [1]}
    follow_up_errors = [
        (error.code, error.message)
        for agent_result in follow_up.agent_results
        for error in agent_result.errors
    ]
    assert follow_up.status == TaskStatus.SUCCESS, follow_up_errors
    with pytest.raises(SessionOwnershipError):
        await orchestrator.run(
            message="Tìm laptop",
            principal_id="user-b",
            session_id=first.session_id,
        )


@pytest.mark.asyncio
async def test_irrelevant_query_does_not_call_agents_or_tools() -> None:
    orchestrator, gateway = build_orchestrator()

    result = await orchestrator.run(
        message="Hãy viết một bài thơ về mặt trăng",
        principal_id="user-a",
        session_id="sess_irrelevant_123",
    )

    assert result.intent == "general.unsupported"
    assert result.status == TaskStatus.SUCCESS
    assert result.plan.steps == ()
    assert result.agent_results == ()
    assert gateway.audit_records() == ()
    assert "chưa gọi tool" in result.answer


@pytest.mark.asyncio
async def test_one_domain_failure_returns_grounded_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, _ = build_orchestrator()
    original_dispatch = orchestrator.executor.dispatcher.dispatch

    async def fail_trust(message: AgentMessage) -> AgentResult:
        if message.target == "trust_agent":
            return AgentResult(
                task_id=message.task_id,
                agent_id="trust_agent",
                status=TaskStatus.FAILED,
                errors=(
                    AgentError(
                        code="trust.simulated_failure",
                        message="Trust Agent tạm thời không khả dụng.",
                        source="trust_agent",
                        retryable=True,
                    ),
                ),
            )
        return await original_dispatch(message)

    monkeypatch.setattr(orchestrator.executor.dispatcher, "dispatch", fail_trust)
    result = await orchestrator.run(
        message="Tìm tai nghe dưới 1 triệu, bán tốt và ít bị khách phàn nàn.",
        principal_id="user-a",
        session_id="sess_partial_123",
    )

    assert result.status == TaskStatus.PARTIAL_SUCCESS
    assert result.selected_product_id is not None
    assert "điểm đa agent" in result.answer
    assert "phần dữ liệu" in result.answer
    assert result.warnings == ("Trust Agent tạm thời không khả dụng.",)


def test_multi_agent_scoring_is_transparent_stable_and_renormalizes_gaps() -> None:
    products = [
        {"id": 1, "name": "A", "price": 100, "ranking_score": 0.8},
        {"id": 2, "name": "B", "price": 90, "ranking_score": 0.8},
    ]
    reviews = [
        {
            "product_id": 1,
            "sentiment": {"distribution": {"positive": 0.5}},
        },
        {
            "product_id": 2,
            "sentiment": {"distribution": {"positive": 0.9}},
        },
    ]
    trusts = [
        {
            "product_id": 1,
            "complaints": {"complaint_rate": 0.1},
            "trust": {"average_trust_score": 0.9},
        }
    ]

    scored = score_recommendation_candidates(products, reviews, trusts)

    assert scored["method"] == "weighted_product_review_trust_v1"
    assert scored["weights"] == {
        "product_fit": 0.55,
        "positive_sentiment": 0.15,
        "complaint_safety": 0.20,
        "review_trust": 0.10,
    }
    assert scored["candidates"][0]["id"] == 2
    assert scored["candidates"][0]["signal_coverage"] == 0.7
    assert scored["candidates"][1]["signal_coverage"] == 0.7
    assert scored["omitted_signals"] == ("complaint_safety", "review_trust")

    tied = score_recommendation_candidates(
        [
            {"id": 2, "name": "B", "price": 90, "ranking_score": 0.8},
            {"id": 1, "name": "A", "price": 100, "ranking_score": 0.8},
        ],
        [],
        [],
    )
    assert [item["id"] for item in tied["candidates"]] == [2, 1]
