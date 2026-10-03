"""Review comparisons preserve each product's sample with a bounded SQL count."""

from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import TaskStatus
from app.models.review import Review
from app.repositories.ecommerce import EcommerceRepository
from tests.test_v2_tools import OBSERVED_AT, operation_for, read_tools, tool_access
from tests.test_v2_tools import tool_sessions as tool_sessions


def add_comparison_reviews(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        session.add_all(
            Review(
                id=100 + index,
                source_id=1,
                external_id=f"comparison-{index}",
                product_id=2,
                rating=4,
                content="Nội dung mẫu so sánh.",
                created_at=OBSERVED_AT + timedelta(minutes=index // 2)
                if index < 12
                else None,
            )
            for index in range(25)
        )
        session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["review.compare", "trust.compare"])
async def test_comparison_reads_reviews_once_for_all_products(
    tool_sessions: sessionmaker[Session], capability: str
) -> None:
    add_comparison_reviews(tool_sessions)
    tools = read_tools(tool_sessions)
    engine = tool_sessions.kw["bind"]
    statements: list[str] = []

    def record_query(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", record_query)
    try:
        result = await tools.execute(
            operation_for(tools, capability, {"product_ids": [2, 1]}),
            tool_access(),
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_query)
    assert result.status == TaskStatus.SUCCESS
    assert result.output is not None
    assert [item["product_id"] for item in result.output["findings"]] == [2, 1]
    counts = [
        fact.value
        for fact in result.evidence.facts
        if fact.field == "sampled_review_count"
    ]
    assert counts == [20, 20]
    review_queries = [
        statement for statement in statements if "FROM reviews" in statement
    ]
    assert len(review_queries) == 1
    product_queries = [
        statement for statement in statements if "WHERE products.id" in statement
    ]
    assert len(product_queries) == 1


def test_batch_review_samples_match_legacy_order_and_per_product_limit(
    tool_sessions: sessionmaker[Session],
) -> None:
    add_comparison_reviews(tool_sessions)
    with tool_sessions() as session:
        repository = EcommerceRepository(session)
        ids = (2, 1, 3, 999)
        expected = {
            product_id: [
                row.id
                for row in repository.get_product_reviews(
                    product_id=product_id, limit=20
                )[1]
            ]
            for product_id in ids
        }
        actual = repository.get_reviews_by_product_ids(ids, limit=20)
        assert list(actual) == list(ids)
        assert {
            product_id: [row.id for row in rows] for product_id, rows in actual.items()
        } == expected
        assert all(row.created_at is not None for row in actual[2][:12])
        assert all(row.created_at is None for row in actual[2][12:])
        assert [row.id for row in actual[2][12:]] == list(range(124, 116, -1))
        assert repository.get_reviews_by_product_ids(()) == {}
