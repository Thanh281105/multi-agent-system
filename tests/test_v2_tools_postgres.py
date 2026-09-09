"""Real PostgreSQL checks for typed read tools over isolated synthetic rows."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import TaskStatus
from app.db.migrate import upgrade_database
from app.models.product import Product
from app.v2.contracts import ConversationMode
from app.v2.registry import ProductResult
from tests.test_v2_tools import (
    operation_for,
    read_tools,
    seed_tool_catalog,
    tool_access,
)
from tests.v2_postgres_support import disposable_postgres_database


@pytest.fixture(scope="module")
def postgres_tool_sessions() -> Iterator[sessionmaker[Session]]:
    with disposable_postgres_database("thanh_v2_p2_read_tools_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions() as session:
            seed_tool_catalog(session)
        try:
            yield sessions
        finally:
            engine.dispose()


@pytest.mark.asyncio
async def test_postgres_author_and_budget_filters_apply_before_limit(
    postgres_tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(postgres_tool_sessions)
    result = await tools.execute(
        operation_for(
            tools,
            "product.catalog.search",
            {"author": "Nguyễn An", "max_price_vnd": 125_000},
        ),
        tool_access(),
    )
    assert result.status == TaskStatus.SUCCESS
    assert [
        item.product_id for item in ProductResult.model_validate(result.output).products
    ] == [2, 1]
    assert all(
        fact.data_version_id == tools.catalog_snapshot.version_id
        for fact in result.evidence.facts
    )


@pytest.mark.asyncio
async def test_postgres_review_order_and_exact_evidence_survive_new_sessions(
    postgres_tool_sessions: sessionmaker[Session],
) -> None:
    first_tools = read_tools(postgres_tool_sessions)
    first = await first_tools.execute(
        operation_for(first_tools, "review.compare", {"product_ids": [1, 2]}),
        tool_access(),
    )
    restarted_tools = read_tools(postgres_tool_sessions)
    second = await restarted_tools.execute(
        operation_for(restarted_tools, "review.compare", {"product_ids": [1, 2]}),
        tool_access(),
    )
    assert first.evidence == second.evidence
    assert [
        fact.value
        for fact in first.evidence.facts
        if fact.field == "sampled_review_count"
    ] == [20, 0]
    assert first.output == second.output


@pytest.mark.asyncio
async def test_postgres_merchant_catalog_does_not_change_history(
    postgres_tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(postgres_tool_sessions)
    with postgres_tool_sessions() as session:
        before = tuple(
            session.execute(
                select(Product.id, Product.price, Product.updated_at).order_by(
                    Product.id
                )
            )
        )
    result = await tools.execute(
        operation_for(tools, "merchant.catalog.read", {"product_ids": [2, 1]}),
        tool_access(mode=ConversationMode.MERCHANT),
    )
    assert result.status == TaskStatus.SUCCESS
    with postgres_tool_sessions() as session:
        after = tuple(
            session.execute(
                select(Product.id, Product.price, Product.updated_at).order_by(
                    Product.id
                )
            )
        )
    assert after == before
