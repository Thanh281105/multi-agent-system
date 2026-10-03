"""Natural request regressions using the unchanged imported public catalog."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import TaskStatus
from app.db.base import Base
from app.db.migrate import upgrade_database
from app.db.public_import import import_public_snapshot
from app.models.product import Product
from app.repositories.book_domain import is_book_product
from app.v2.history import ModelContext
from app.v2.planning import BoundedV2Planner, PlanningContext, RuntimeDataVersions
from app.v2.registry import CatalogSearchInput, ProductResult, ReviewResult
from app.v2.sandbox_seed import seed_demo_offers
from app.v2.tools import V2ReadTools
from tests.test_v2_tools import operation_for, read_tools, tool_access
from tests.v2_postgres_support import disposable_postgres_database

SNAPSHOT = Path(__file__).resolve().parents[1] / "data/snapshots/tiki-books-v4-eval"


@pytest.fixture(
    params=["sqlite", pytest.param("postgresql", marks=pytest.mark.integration)]
)
def actual_catalog_sessions(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[sessionmaker[Session]]:
    def imported(url: str) -> Iterator[sessionmaker[Session]]:
        engine = create_engine(url)
        if request.param == "sqlite":
            Base.metadata.create_all(engine)
        else:
            upgrade_database(url)
        sessions = sessionmaker(engine, expire_on_commit=False)
        import_public_snapshot(snapshot_dir=SNAPSHOT, session_factory=sessions)
        try:
            yield sessions
        finally:
            engine.dispose()

    if request.param == "sqlite":
        yield from imported(f"sqlite+pysqlite:///{(tmp_path / 'actual.db').as_posix()}")
    else:
        with disposable_postgres_database("thanh_v2_p2_catalog_fix_") as url:
            yield from imported(url)


def planning_context(tools: V2ReadTools) -> PlanningContext:
    return PlanningContext(
        access=tool_access(),
        versions=RuntimeDataVersions(
            catalog_version_id=tools.catalog_snapshot.version_id,
            corpus_version_id="corpus_test",
            index_manifest_id="index_test",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Tìm 3 sách dưới 300k, rating ít nhất 4.5, số trang tối thiểu 300",
        "Tìm 3 sách giá không vượt quá 300000 VND, ít nhất 4,5 sao, ít nhất 300 trang",
        "Tìm 3 sách dưới 300k, rating >= 4.5, số trang >= 300",
    ],
)
async def test_numeric_constraints_reach_actual_catalog(
    actual_catalog_sessions: sessionmaker[Session],
    message: str,
) -> None:
    tools = read_tools(actual_catalog_sessions)
    planned = await BoundedV2Planner().plan(message, planning_context(tools))
    assert planned.clarification_code is None
    operation = planned.initial_operations[0]
    assert operation.parameters["max_price_vnd"] == 300_000
    assert operation.parameters["min_price_vnd"] is None
    assert operation.parameters["min_rating"] == 4.5
    assert operation.parameters["min_page_count"] == 300
    result = await tools.execute(operation, tool_access())
    assert result.status == TaskStatus.SUCCESS
    candidates = ProductResult.model_validate(result.output).products
    assert len(candidates) == 3
    with actual_catalog_sessions() as session:
        for candidate in candidates:
            product = session.get(Product, candidate.product_id)
            assert product is not None and product.price <= 300_000
            assert product.rating is not None and product.rating >= 4.5
            assert product.page_count is not None and product.page_count >= 300


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        'So sánh "Sapiens" và "Steve Jobs"',
        "So sánh Sapiens và Steve Jobs",
    ],
)
async def test_fresh_two_title_comparison_resolves_each_catalog_entity(
    actual_catalog_sessions: sessionmaker[Session],
    message: str,
) -> None:
    tools = read_tools(actual_catalog_sessions)
    planner = BoundedV2Planner()
    context = planning_context(tools)
    planned = await planner.plan(message, context)
    assert planned.clarification_code is None
    assert planned.entity_queries == ("Sapiens", "Steve Jobs")
    initial = await tools.execute(planned.initial_operations[0], tool_access())
    assert initial.status == TaskStatus.SUCCESS
    candidates = ProductResult.model_validate(initial.output).products
    assert len(candidates) == 2
    assert "Sapiens" in candidates[0].title and "Steve Jobs" in candidates[1].title
    operations = planner.bind_initial_candidates(
        planned,
        tuple(row.product_id for row in candidates),
        context,
        planned.initial_operations,
    )
    comparison = next(row for row in operations if row.capability == "product.compare")
    result = await tools.execute(comparison, tool_access())
    assert result.status == TaskStatus.SUCCESS
    assert {fact.subject_id for fact in result.evidence.facts} == {
        f"product_{row.product_id}" for row in candidates
    }
    with actual_catalog_sessions() as session:
        previous_ids = tuple(
            session.scalars(select(Product.id).order_by(Product.id).limit(2))
        )
    previous_context = context.model_copy(
        update={
            "model_context": ModelContext(referenced_product_ids=previous_ids),
        }
    )
    refreshed = await planner.plan(message, previous_context)
    assert "product.compare" in refreshed.deferred_capabilities
    rebound = planner.bind_initial_candidates(
        refreshed,
        tuple(row.product_id for row in candidates),
        previous_context,
        refreshed.initial_operations,
    )
    assert next(
        row for row in rebound if row.capability == "product.compare"
    ).parameters["product_ids"] == [row.product_id for row in candidates]


@pytest.mark.asyncio
async def test_rating_and_page_upper_bounds_reach_actual_catalog(
    actual_catalog_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(actual_catalog_sessions)
    planned = await BoundedV2Planner().plan(
        "Tìm 2 sách rating tối đa 4.5, số trang tối đa 300", planning_context(tools)
    )
    assert planned.clarification_code is None
    assert planned.min_price_vnd is None and planned.max_price_vnd is None
    result = await tools.execute(planned.initial_operations[0], tool_access())
    assert result.status == TaskStatus.SUCCESS
    candidates = ProductResult.model_validate(result.output).products
    assert len(candidates) == 2
    with actual_catalog_sessions() as session:
        for row in candidates:
            product = session.get(Product, row.product_id)
            assert product is not None
            assert product.rating is not None and product.rating <= 4.5
            assert product.page_count is not None and product.page_count <= 300


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "title"),
    [
        ("Phân tích review của Sapiens", "Sapiens"),
        ("Khách hàng đánh giá sách Nhật Ký Tarot thế nào?", "Nhật Ký Tarot"),
        ("Người mua nhận xét sách Nhật Ký Tarot ra sao?", "Nhật Ký Tarot"),
        ('Khách hàng đánh giá sách "Nhật Ký Tarot" như thế nào?', "Nhật Ký Tarot"),
    ],
)
async def test_natural_review_resolves_title_and_reads_its_sample(
    actual_catalog_sessions: sessionmaker[Session],
    message: str,
    title: str,
) -> None:
    tools = read_tools(actual_catalog_sessions)
    planner = BoundedV2Planner()
    context = planning_context(tools)
    planned = await planner.plan(message, context)
    assert planned.catalog_query == title
    assert planned.entity_queries == (title,)
    initial = await tools.execute(planned.initial_operations[0], tool_access())
    assert initial.status == TaskStatus.SUCCESS
    candidates = ProductResult.model_validate(initial.output).products
    assert len(candidates) == 1 and title in candidates[0].title
    operations = planner.bind_initial_candidates(
        planned,
        (candidates[0].product_id,),
        context,
        planned.initial_operations,
    )
    review = next(row for row in operations if row.capability.startswith("review."))
    result = await tools.execute(review, tool_access())
    assert result.status == TaskStatus.SUCCESS
    findings = ReviewResult.model_validate(result.output).findings
    assert findings[0].product_id == candidates[0].product_id
    assert 0 < findings[0].review_count <= 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Gợi ý sách từ 350k đến 400k dựa trên review và độ tin cậy",
        "Gợi ý sách từ 350k đến 400k theo đánh giá và độ tin cậy",
    ],
)
async def test_review_based_recommendation_keeps_price_bounds_and_reads(
    actual_catalog_sessions: sessionmaker[Session],
    message: str,
) -> None:
    tools = read_tools(actual_catalog_sessions)
    planner = BoundedV2Planner()
    context = planning_context(tools)
    planned = await planner.plan(message, context)
    assert planned.catalog_query is None and not planned.entity_queries
    assert planned.min_price_vnd == 350_000 and planned.max_price_vnd == 400_000
    assert planned.initial_operations[0].capability == "product.rank"
    initial = await tools.execute(planned.initial_operations[0], tool_access())
    assert initial.status == TaskStatus.SUCCESS
    candidates = ProductResult.model_validate(initial.output).products
    assert candidates
    with actual_catalog_sessions() as session:
        for candidate in candidates:
            product = session.get(Product, candidate.product_id)
            assert product is not None and 350_000 <= product.price <= 400_000
            assert is_book_product(product)
    operations = planner.bind_initial_candidates(
        planned,
        tuple(candidate.product_id for candidate in candidates),
        context,
        planned.initial_operations,
    )
    capabilities = {operation.capability for operation in operations}
    assert capabilities & {"review.retrieve", "review.compare"}
    assert capabilities & {"trust.analyze", "trust.compare"}
    for operation in operations:
        result = await tools.execute(operation, tool_access())
        assert result.status == TaskStatus.SUCCESS


@pytest.mark.asyncio
async def test_book_guard_covers_ranking_statistics_direct_ids_and_seed(
    actual_catalog_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(actual_catalog_sessions)
    planned = await BoundedV2Planner().plan(
        "Gợi ý sách khoảng 350–400k", planning_context(tools)
    )
    assert planned.min_price_vnd == 350_000 and planned.max_price_vnd == 400_000
    result = await tools.execute(planned.initial_operations[0], tool_access())
    assert result.status == TaskStatus.SUCCESS
    candidates = ProductResult.model_validate(result.output).products
    assert candidates
    with actual_catalog_sessions() as session:
        products = tuple(session.scalars(select(Product)))
        books = tuple(row for row in products if is_book_product(row))
        excluded = tuple(row for row in products if not is_book_product(row))
        assert len(products) == 200 and len(books) == 199 and len(excluded) == 1
        for row in candidates:
            product = session.get(Product, row.product_id)
            assert product is not None and is_book_product(product)
    market = await tools.execute(
        operation_for(tools, "market.snapshot", {"dimension": "category"}),
        tool_access(),
    )
    assert next(
        fact.value
        for fact in market.evidence.facts
        if fact.field == "snapshot_product_count"
    ) == len(books)
    direct = await tools.execute(
        operation_for(tools, "product.compare", {"product_ids": [excluded[0].id]}),
        tool_access(),
    )
    assert direct.status == TaskStatus.FAILED
    assert direct.error is not None and direct.error.code == "catalog_product_not_found"
    seeded = seed_demo_offers(actual_catalog_sessions, tenant_id="default")
    assert seeded.inserted == len(books)


@pytest.mark.asyncio
async def test_unresolved_or_ambiguous_title_does_not_bind_an_arbitrary_edition(
    actual_catalog_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(actual_catalog_sessions)
    with actual_catalog_sessions() as session:
        original = session.scalars(
            select(Product).where(Product.name.like("Sapiens%"))
        ).one()
        session.add(
            Product(
                source_id=original.source_id,
                external_id="synthetic-edition-regression",
                name="Sapiens (Synthetic additional edition)",
                category=original.category,
                publisher=original.publisher,
                page_count=original.page_count,
                price=original.price,
                rating=original.rating,
                description="Synthetic fixture",
                platform="Tiki",
            )
        )
        session.commit()
    for queries, code in [
        (("Sapiens", "Steve Jobs"), "catalog_entity_ambiguous"),
        (("No matching fixture title", "Steve Jobs"), "catalog_entity_not_found"),
    ]:
        result = await tools.execute(
            operation_for(tools, "product.catalog.search", {"entity_queries": queries}),
            tool_access(),
        )
        assert result.status == TaskStatus.FAILED
        assert result.error is not None and result.error.code == code
        assert not result.evidence.facts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", ["Tìm điện thoại Samsung", "Gợi ý laptop dưới 20 triệu"]
)
async def test_explicit_unsupported_product_is_refused_before_reads(
    message: str,
) -> None:
    context = PlanningContext(
        access=tool_access(),
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_test",
            corpus_version_id="corpus_test",
            index_manifest_id="index_test",
        ),
    )
    planned = await BoundedV2Planner().plan(message, context)
    assert planned.intent == "general.unsupported"
    assert planned.clarification_code == "book_domain_unsupported"
    assert not planned.initial_operations


@pytest.mark.parametrize(
    "parameters",
    [
        {"min_rating": 5.1},
        {"min_rating": 4.5, "max_rating": 4},
        {"min_page_count": 0},
        {"min_page_count": 500, "max_page_count": 200},
    ],
)
def test_invalid_typed_catalog_bounds_are_rejected(
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CatalogSearchInput.model_validate(parameters)
