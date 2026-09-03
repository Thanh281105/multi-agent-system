"""End-to-end domain agent tests through Agent Gateway and MCP boundaries."""

from __future__ import annotations

import pytest

from app.agent_gateway import AgentGateway
from app.agents import DEFAULT_AGENT_IDS, AgentDispatcher, build_default_dispatcher
from app.agents.product.skills import rank_products
from app.agents.review.skills import extract_review_aspects
from app.agents.trust.skills import analyze_review_trust
from app.contracts import AgentMessage, AuthorizationContext, TaskStatus
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
        authorization=AuthorizationContext(
            principal_id="test-principal",
            scopes=frozenset({"ecommerce.read"}),
        ),
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
async def test_book_catalog_agent_searches_and_ranks_snapshot_facts(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, gateway = runtime

    result = await dispatcher.dispatch(
        message(
            target="product_agent",
            action="product.rank",
            payload={
                "publisher": "Nhà Xuất Bản Thế Giới",
                "max_price": 200_000,
                "min_rating": 4.5,
            },
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["search"]["count"] == 6
    assert result.data["ranking"]["count"] == 6
    assert result.data["ranking"]["products"][0]["ranking_score"] >= 0
    assert all(
        product["publisher"] == "Nhà Xuất Bản Thế Giới"
        for product in result.data["ranking"]["products"]
    )
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
    assert "đóng gói/giao hàng" in result.data["aspects"]["negative_aspects"]
    assert "5 review đã làm sạch" in result.data["summary"]
    assert "303 review" in result.data["summary"]
    assert all(provenance.sample_data for provenance in result.provenance)


@pytest.mark.asyncio
async def test_review_agent_uses_book_complaint_taxonomy(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, _ = runtime

    wrong_book = await dispatcher.dispatch(
        message(
            target="review_agent",
            action="review.summarize",
            payload={"product_id": 6},
        )
    )
    mixed_feedback = await dispatcher.dispatch(
        message(
            target="review_agent",
            action="review.summarize",
            payload={"product_id": 9},
        )
    )

    assert "sai/thiếu sách hoặc tập" in wrong_book.data["aspects"]["negative_aspects"]
    assert "dịch thuật/biên tập" in mixed_feedback.data["aspects"]["negative_aspects"]

    mixed_sentence = extract_review_aspects(
        [
            {
                "id": 1,
                "rating": 3,
                "content": (
                    "Sách đẹp, giấy tốt, tuy nhiên văn dịch không được cuốn hút."
                ),
            }
        ]
    )
    assert "dịch thuật/biên tập" in mixed_sentence["negative_aspects"]
    assert "giấy/in" not in mixed_sentence["negative_aspects"]


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
    assert result.data["trust"]["method"].endswith("rules_v2")
    assert result.data["trust"]["authenticity_assessed"] is False
    assert "spam_probability" not in result.model_dump_json()
    assert "suspected_spam" not in result.model_dump_json()
    assert result.data["complaints"]["complaint_count"] >= 1
    assert 0 <= result.data["complaints"]["complaint_rate"] <= 1


def test_trust_rules_keep_duplicate_short_generic_signals_non_conclusive() -> None:
    result = analyze_review_trust(
        [
            {"id": 1, "rating": 5, "content": "Sách hay"},
            {"id": 2, "rating": 5, "content": "Sách hay"},
        ]
    )

    assert result["flagged_text_quality_count"] == 2
    assert result["authenticity_assessed"] is False
    assert result["items"][0]["signals"] == [
        "duplicate_text",
        "very_short",
        "generic_only",
    ]
    serialized = str(result)
    assert "spam_probability" not in serialized
    assert "suspected_spam" not in serialized
    assert "không xác định review giả" in result["limitation"]


def test_trust_rules_preserve_non_empty_symbol_only_reviews() -> None:
    result = analyze_review_trust(
        [{"id": 1, "rating": 5, "content": "😍😍"}],
    )

    assert result["count"] == 1
    assert result["items"][0]["signals"] == ["very_short"]
    assert result["authenticity_assessed"] is False


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
async def test_market_agent_returns_only_cross_sectional_snapshot_statistics(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, gateway = runtime

    result = await dispatcher.dispatch(
        message(
            target="market_agent",
            action="market.analyze",
            payload={},
        )
    )

    assert result.status == TaskStatus.SUCCESS
    assert result.data["market"]["representative_of_real_market"] is False
    assert result.data["market"]["live_market_data"] is False
    assert result.data["market"]["trend_analysis"] is False
    statistics = result.data["market"]["statistics"]
    assert statistics["product_count"] == 24
    assert statistics["min_price"] == 38_700
    assert statistics["max_price"] == 292_500
    assert statistics["average_price"] == 130_711.29
    assert statistics["average_rating"] == 4.504
    assert statistics["publisher_distribution"]["Nhà Xuất Bản Thế Giới"] == 6
    assert statistics["snapshot_sources"][0]["dataset_id"] == "tiki-books"
    assert [record.tool_name for record in gateway.audit_records()] == [
        "analyze_market"
    ]
    assert all(provenance.sample_data for provenance in result.provenance)


@pytest.mark.asyncio
async def test_market_agent_filters_cross_section_by_publisher(
    runtime: tuple[AgentDispatcher, AgentGateway],
) -> None:
    dispatcher, gateway = runtime

    result = await dispatcher.dispatch(
        message(
            target="market_agent",
            action="market.analyze",
            payload={"publisher": "nhà xuất bản thế giới"},
        )
    )

    statistics = result.data["market"]["statistics"]
    assert statistics["product_count"] == 6
    assert statistics["filters"]["publisher"] == "nhà xuất bản thế giới"
    assert statistics["provenance"][0]["source_id"] == ("tiki-books:kaggle-v4:test")
    assert [record.tool_name for record in gateway.audit_records()] == [
        "analyze_market"
    ]


def test_book_ranking_handles_nullable_snapshot_rating_and_popularity() -> None:
    result = rank_products(
        [
            {
                "id": 1,
                "name": "Sách thiếu tín hiệu",
                "price": 100_000,
                "rating": None,
                "sold_count": None,
            },
            {
                "id": 2,
                "name": "Sách đủ tín hiệu",
                "price": 120_000,
                "rating": 4.8,
                "sold_count": 100,
            },
        ]
    )

    assert result["count"] == 2
    assert result["products"][0]["id"] == 2
    missing = next(item for item in result["products"] if item["id"] == 1)
    assert missing["missing_ranking_facts"] == ["rating", "sold_count"]


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


def test_dispatcher_can_build_a_validated_agent_ablation() -> None:
    gateway = AgentGateway(router=build_default_mcp_router())

    dispatcher = build_default_dispatcher(
        gateway,
        enabled_agents=("product_agent", "trust_agent"),
    )

    assert DEFAULT_AGENT_IDS == (
        "product_agent",
        "review_agent",
        "trust_agent",
        "market_agent",
    )
    assert dispatcher.agent_ids() == ("product_agent", "trust_agent")
    with pytest.raises(ValueError, match="unknown enabled agents"):
        build_default_dispatcher(gateway, enabled_agents=("unknown_agent",))
    with pytest.raises(ValueError, match="at least one agent"):
        build_default_dispatcher(gateway, enabled_agents=())
