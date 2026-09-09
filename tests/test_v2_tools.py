"""Behavioral checks for the deterministic, shared v2 read boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import TaskStatus
from app.db.base import Base
from app.knowledge.service import KnowledgeService
from app.knowledge.v2_contracts import (
    IndexBuildSpec,
    PublishedKnowledgeSnapshot,
    RetrievalPolicy,
    content_addressed_id,
    stable_evidence_id,
)
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review
from app.v2.authorization import (
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceBinding,
    ResourceNotFoundError,
)
from app.v2.contracts import ConversationMode, EvidenceKind, EvidenceReference
from app.v2.registry import KnowledgeExcerpt, KnowledgeResult, ProductResult
from app.v2.runtime_contracts import RuntimeOperation, build_operation_key
from app.v2.tools import V2ReadTools, load_catalog_snapshot

OBSERVED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)


def seed_tool_catalog(session: Session) -> None:
    """Small synthetic rows; no imported snapshot or user data is read."""
    source = DatasetSource(
        id=1,
        dataset_id="v2-tool-fixture",
        dataset_version="2026-08-30",
        profile="test",
        source_url="https://example.test/synthetic-books",
        source_license="Synthetic test fixture",
        source_revision=1,
        raw_archive_sha256="a" * 64,
        snapshot_sha256="b" * 64,
        products_sha256="c" * 64,
        reviews_sha256="d" * 64,
        sampling_seed=1,
        product_count=8,
        review_count=25,
        retrieved_at=OBSERVED_AT,
    )
    session.add(source)
    session.flush()
    session.add_all(
        [
            Product(
                id=index,
                source_id=source.id,
                external_id=f"synthetic-{index}",
                name=f"Sách thử nghiệm {index}",
                authors=["Nguyễn An" if index <= 5 else "Tác giả khác"],
                publisher="Nhà xuất bản mẫu",
                category="Công nghệ" if index <= 4 else "Văn học",
                page_count=100 + index * 10,
                price=100_000 + index * 10_000,
                rating=4.0 + index / 10,
                sold_count=100 + index,
                source_review_count=25 if index == 1 else 0,
                description="Synthetic book description.",
                platform="Tiki",
            )
            for index in range(1, 9)
        ]
    )
    session.flush()
    session.add_all(
        [
            Review(
                id=index,
                source_id=source.id,
                external_id=f"review-{index}",
                product_id=1,
                rating=5 if index % 2 == 0 else 2,
                content="nội dung rất hay RAW_REVIEW_MARKER"
                if index % 2 == 0
                else "bìa bị móp RAW_REVIEW_MARKER",
                created_at=OBSERVED_AT + timedelta(minutes=index),
            )
            for index in range(1, 26)
        ]
    )
    session.commit()


@pytest.fixture
def tool_sessions(tmp_path: Path) -> Any:
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'tool-fixtures.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        seed_tool_catalog(session)
    try:
        yield sessions
    finally:
        engine.dispose()


def tool_access(
    *,
    mode: ConversationMode = ConversationMode.SHOPPER,
    scopes: frozenset[str] | None = None,
    store_id: str = "demo",
) -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="default", principal_id="tool-user", mode=mode, store_id=store_id
        ),
        scopes=scopes
        if scopes is not None
        else frozenset({"ecommerce.read", "merchant.read"}),
    )


def operation_for(
    tools: V2ReadTools,
    capability: str,
    parameters: dict[str, Any],
    *,
    versions: tuple[str, ...] | None = None,
) -> RuntimeOperation:
    definition = tools.registry.capability(capability)
    canonical = definition.validate_input(parameters).model_dump(mode="json")
    bound_versions = versions or tools.data_version_ids(capability)
    return RuntimeOperation(
        step_id="step_read_fixture",
        capability=capability,
        service=definition.service,
        parameters=canonical,
        data_version_ids=bound_versions,
        operation_key=build_operation_key(capability, canonical, bound_versions),
    )


def read_tools(sessions: sessionmaker[Session]) -> V2ReadTools:
    return V2ReadTools(sessions, catalog_snapshot=load_catalog_snapshot(sessions))


@pytest.mark.asyncio
async def test_catalog_filters_precede_candidate_limit_and_keep_exact_source(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    operation = operation_for(
        tools,
        "product.catalog.search",
        {"author": "Nguyễn An", "max_price_vnd": 125_000},
    )
    result = await tools.execute(operation, tool_access())
    assert result.status == TaskStatus.SUCCESS
    output = ProductResult.model_validate(result.output)
    assert [product.product_id for product in output.products] == [2, 1]
    assert all(
        product.price_vnd is not None and product.price_vnd <= 125_000
        for product in output.products
    )
    assert {reference.observed_at for reference in result.evidence.references} == {
        OBSERVED_AT
    }
    assert {
        fact.value
        for fact in result.evidence.facts
        if fact.field == "snapshot_price_vnd"
    } == {110_000, 120_000}
    assert all("lịch sử" in reference.title for reference in result.evidence.references)
    for fact in result.evidence.facts:
        assert type(fact.value) is int if fact.field == "snapshot_price_vnd" else True
        excerpt = next(
            item
            for item in result.evidence.excerpts
            if item.evidence_id in fact.evidence_ids
        )
        assert excerpt.subject_ids == (fact.subject_id,)
        assert (
            excerpt.source_version_id
            == fact.data_version_id
            == tools.catalog_snapshot.version_id
        )
    replay = await tools.execute(operation, tool_access())
    assert replay.evidence == result.evidence


@pytest.mark.asyncio
async def test_compare_preserves_resolved_order_and_rejects_missing_product(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    result = await tools.execute(
        operation_for(tools, "product.compare", {"product_ids": [3, 1]}), tool_access()
    )
    assert [
        product.product_id
        for product in ProductResult.model_validate(result.output).products
    ] == [3, 1]
    missing = await tools.execute(
        operation_for(tools, "product.compare", {"product_ids": [1, 999]}),
        tool_access(),
    )
    assert missing.status == TaskStatus.FAILED
    assert (
        missing.error is not None and missing.error.code == "catalog_product_not_found"
    )
    assert missing.output is None and not missing.evidence.references


@pytest.mark.asyncio
async def test_empty_search_does_not_invent_evidence_or_products(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    result = await tools.execute(
        operation_for(
            tools, "product.catalog.search", {"query": "No such synthetic title"}
        ),
        tool_access(),
    )
    assert result.status == TaskStatus.SUCCESS
    assert ProductResult.model_validate(result.output).products == ()
    assert not result.evidence.facts and not result.evidence.references


@pytest.mark.asyncio
async def test_revoked_mode_and_store_rejected_before_database_io(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    tools.session_factory = Mock(
        side_effect=AssertionError("authorization must precede database access")
    )
    operation = operation_for(tools, "merchant.catalog.read", {})
    with pytest.raises(AuthorizationDeniedError):
        await tools.execute(operation, tool_access())
    with pytest.raises(AuthorizationDeniedError):
        await tools.execute(
            operation,
            tool_access(
                mode=ConversationMode.MERCHANT, scopes=frozenset({"ecommerce.read"})
            ),
        )
    with pytest.raises(ResourceNotFoundError):
        await tools.execute(
            operation, tool_access(mode=ConversationMode.MERCHANT, store_id="foreign")
        )
    tools.session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_write_capability_cannot_cross_read_boundary(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    tools.session_factory = Mock(
        side_effect=AssertionError("writes are not read tools")
    )
    operation = operation_for(
        tools,
        "shopper.cart.execute",
        {
            "cart_id": "cart_fixture",
            "product_id": 1,
            "quantity": 2,
            "expected_version": 1,
        },
    )
    result = await tools.execute(
        operation, tool_access(scopes=frozenset({"ecommerce.read", "ecommerce.write"}))
    )
    assert result.error is not None and result.error.code == "read_tool_write_forbidden"
    tools.session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_mutable_parameters_and_wrong_versions_fail_before_io(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    tools.session_factory = Mock(
        side_effect=AssertionError("invalid operation must not dispatch")
    )
    operation = operation_for(tools, "product.compare", {"product_ids": [1]})
    operation.parameters["product_ids"] = [2]
    with pytest.raises(ValidationError, match="operation key"):
        await tools.execute(operation, tool_access())
    stale = operation_for(
        tools, "product.compare", {"product_ids": [1]}, versions=("cat_other",)
    )
    result = await tools.execute(stale, tool_access())
    assert result.error is not None and result.error.code == "stale_data_version"
    tools.session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_changed_source_manifest_invalidates_pinned_catalog(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    operation = operation_for(tools, "product.compare", {"product_ids": [1]})
    with tool_sessions() as session:
        source = session.get(DatasetSource, 1)
        assert source is not None
        source.snapshot_sha256 = "e" * 64
        session.commit()
    result = await tools.execute(operation, tool_access())
    assert result.status == TaskStatus.FAILED
    assert result.error is not None and result.error.code == "stale_data_version"
    assert not result.evidence.facts


@pytest.mark.asyncio
async def test_review_and_trust_use_same_bounded_sample_without_authenticity_claim(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    review = await tools.execute(
        operation_for(tools, "review.retrieve", {"product_ids": [1]}), tool_access()
    )
    trust = await tools.execute(
        operation_for(tools, "trust.analyze", {"product_ids": [1]}), tool_access()
    )
    assert review.status == trust.status == TaskStatus.SUCCESS
    review_facts = {fact.field: fact.value for fact in review.evidence.facts}
    trust_facts = {fact.field: fact.value for fact in trust.evidence.facts}
    assert (
        review_facts["sampled_review_count"]
        == trust_facts["sampled_review_count"]
        == 20
    )
    assert review_facts["sampled_average_rating"] == Decimal("3.500")
    assert trust_facts["complaint_count"] == 10
    assert "không xác định review giả" in str(trust_facts["trust_limitation"])
    assert "RAW_REVIEW_MARKER" not in review.model_dump_json() + trust.model_dump_json()
    assert all(
        reference.kind == EvidenceKind.REVIEW
        for reference in review.evidence.references
    )
    assert all(
        reference.kind == EvidenceKind.TRUST for reference in trust.evidence.references
    )


@pytest.mark.asyncio
async def test_no_reviews_are_reported_as_empty_sample(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    result = await tools.execute(
        operation_for(tools, "review.retrieve", {"product_ids": [2]}), tool_access()
    )
    facts = {fact.field: fact.value for fact in result.evidence.facts}
    assert facts == {"sampled_review_count": 0}
    assert (
        result.output is not None
        and result.output["findings"][0]["average_rating"] is None
    )  # type: ignore[index]


@pytest.mark.asyncio
async def test_rank_keeps_base_evidence_identity_and_adds_separate_score(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    search = await tools.execute(
        operation_for(tools, "product.catalog.search", {"author": "Nguyễn An"}),
        tool_access(),
    )
    ranked = await tools.execute(
        operation_for(tools, "product.rank", {"author": "Nguyễn An"}), tool_access()
    )
    assert len(ProductResult.model_validate(ranked.output).products) == 5
    base_ids = {reference.evidence_id for reference in search.evidence.references}
    assert base_ids <= {
        reference.evidence_id for reference in ranked.evidence.references
    }
    scores = [fact for fact in ranked.evidence.facts if fact.field == "ranking_score"]
    assert len(scores) == 5 and all(isinstance(fact.value, Decimal) for fact in scores)
    assert all(
        "nhóm ứng viên" in reference.title
        for reference in ranked.evidence.references
        if reference.evidence_id not in base_ids
    )


@pytest.mark.asyncio
async def test_market_facts_keep_global_scope_and_historical_counts(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    result = await tools.execute(
        operation_for(
            tools, "market.snapshot", {"dimension": "category", "author": "Nguyễn An"}
        ),
        tool_access(),
    )
    assert result.status == TaskStatus.SUCCESS
    assert result.output is not None
    assert result.output["metrics"] == [
        {"label": "Công nghệ", "count": 4, "value": None},
        {"label": "Văn học", "count": 1, "value": None},
    ]
    assert all(fact.subject_id is None for fact in result.evidence.facts)
    assert all(excerpt.subject_ids == () for excerpt in result.evidence.excerpts)
    assert (
        next(
            fact.value
            for fact in result.evidence.facts
            if fact.field == "snapshot_product_count"
        )
        == 5
    )


@pytest.mark.asyncio
async def test_merchant_reads_catalog_but_does_not_invent_inventory(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    access = tool_access(mode=ConversationMode.MERCHANT)
    catalog = await tools.execute(
        operation_for(tools, "merchant.catalog.read", {}), access
    )
    assert len(ProductResult.model_validate(catalog.output).products) == 5
    inventory = await tools.execute(
        operation_for(tools, "merchant.inventory.read", {}), access
    )
    assert (
        inventory.error is not None
        and inventory.error.code == "read_capability_unavailable"
    )
    assert inventory.output is None


def _knowledge_fixture() -> tuple[
    KnowledgeService,
    PublishedKnowledgeSnapshot,
    KnowledgeResult,
    tuple[EvidenceReference, ...],
]:
    spec = IndexBuildSpec(
        embedding_model="synthetic-embedding",
        embedding_dimension=32,
        chunker_version="table_chunker_v1",
        enrichment_policy_version="source_keywords_v1",
    )
    snapshot = PublishedKnowledgeSnapshot(
        corpus_version_id=content_addressed_id("cor", {"test": "read-tools"}),
        corpus_name="tools-fixture",
        corpus_version="1",
        index_manifest_id=content_addressed_id("idx", {"test": "read-tools"}),
        index_fingerprint=spec.index_fingerprint,
        embedding_model=spec.embedding_model,
        embedding_dimension=spec.embedding_dimension,
        chunker_version=spec.chunker_version,
        enrichment_policy_version=spec.enrichment_policy_version,
        query_embedding_fingerprint=spec.query_embedding_fingerprint,
        retrieval_policy=RetrievalPolicy(
            version="synthetic_v1",
            dense_weight=1.0,
            lexical_weight=1.0,
            min_dense_relevance=0.25,
            min_lexical_coverage=0.10,
        ),
        published_at=OBSERVED_AT,
    )
    references: list[EvidenceReference] = []
    excerpts: list[KnowledgeExcerpt] = []
    for index in (1, 2):
        source_id = f"src_synthetic_{index}"
        version = content_addressed_id("svr", {"source": index})
        chunk = content_addressed_id("chk", {"source": index})
        span = content_addressed_id("spn", {"source": index})
        evidence_id = stable_evidence_id(
            source_id=source_id, source_version_id=version, chunk_id=chunk, span_id=span
        )
        excerpts.append(
            KnowledgeExcerpt(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=version,
                chunk_id=chunk,
                span_id=span,
                excerpt=f"Exact synthetic source {index}.",
                score=0.8,
            )
        )
        references.append(
            EvidenceReference(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=version,
                chunk_id=chunk,
                span_id=span,
                display_label=f"[C{index}]",
                kind=EvidenceKind.KNOWLEDGE,
                title=f"Synthetic source {index}",
                observed_at=OBSERVED_AT,
            )
        )
    result = KnowledgeResult(
        corpus_version_id=snapshot.corpus_version_id,
        excerpts=tuple(excerpts),
        answerable=True,
    )
    service = Mock(spec=KnowledgeService)
    service.retrieve = AsyncMock(
        return_value=SimpleNamespace(result=result, evidence=tuple(references))
    )
    service.store = Mock()
    service.store.list_authorized_sources.side_effect = (
        lambda snapshot, access, *, product_ids: (
            SimpleNamespace(source_id=f"src_synthetic_{product_ids[0]}"),
        )
    )
    return cast(KnowledgeService, service), snapshot, result, tuple(references)


@pytest.mark.asyncio
async def test_knowledge_propagates_pins_access_and_maps_only_exact_authorized_sources(
    tool_sessions: sessionmaker[Session],
) -> None:
    service, snapshot, _, references = _knowledge_fixture()
    tools = V2ReadTools(
        tool_sessions,
        catalog_snapshot=load_catalog_snapshot(tool_sessions),
        knowledge_service=service,
        knowledge_snapshot=snapshot,
    )
    request = operation_for(
        tools,
        "knowledge.retrieve",
        {"query": "chủ đề của hai sách", "product_ids": [1, 2]},
    )
    result = await tools.execute(request, tool_access())
    assert result.status == TaskStatus.SUCCESS
    assert result.evidence.references == references
    assert [excerpt.subject_ids for excerpt in result.evidence.excerpts] == [
        ("product_1",),
        ("product_2",),
    ]
    assert not result.evidence.facts
    service.retrieve.assert_awaited_once()  # type: ignore[attr-defined]
    _, kwargs = service.retrieve.await_args  # type: ignore[attr-defined]
    assert kwargs == {
        "corpus_version_id": snapshot.corpus_version_id,
        "index_manifest_id": snapshot.index_manifest_id,
    }
    assert service.retrieve.await_args.args[1] == tool_access()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_invalid_product_prevents_knowledge_provider_call(
    tool_sessions: sessionmaker[Session],
) -> None:
    service, snapshot, _, _ = _knowledge_fixture()
    tools = V2ReadTools(
        tool_sessions,
        catalog_snapshot=load_catalog_snapshot(tool_sessions),
        knowledge_service=service,
        knowledge_snapshot=snapshot,
    )
    result = await tools.execute(
        operation_for(
            tools, "knowledge.retrieve", {"query": "query", "product_ids": [999]}
        ),
        tool_access(),
    )
    assert result.error is not None and result.error.code == "catalog_product_not_found"
    service.retrieve.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_knowledge_runtime_failure_is_not_converted_to_success(
    tool_sessions: sessionmaker[Session],
) -> None:
    service, snapshot, _, _ = _knowledge_fixture()
    service.retrieve.side_effect = RuntimeError("synthetic provider failure")  # type: ignore[attr-defined]
    tools = V2ReadTools(
        tool_sessions,
        catalog_snapshot=load_catalog_snapshot(tool_sessions),
        knowledge_service=service,
        knowledge_snapshot=snapshot,
    )
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        await tools.execute(
            operation_for(tools, "knowledge.retrieve", {"query": "query"}),
            tool_access(),
        )


@pytest.mark.asyncio
async def test_reads_leave_imported_rows_unchanged(
    tool_sessions: sessionmaker[Session],
) -> None:
    tools = read_tools(tool_sessions)
    with tool_sessions() as session:
        before = tuple(
            session.execute(select(Product.id, Product.price, Product.updated_at)).all()
        )
    await tools.execute(
        operation_for(tools, "product.rank", {"category": "Văn học"}), tool_access()
    )
    await tools.execute(
        operation_for(tools, "trust.analyze", {"product_ids": [1]}), tool_access()
    )
    with tool_sessions() as session:
        after = tuple(
            session.execute(select(Product.id, Product.price, Product.updated_at)).all()
        )
    assert after == before
