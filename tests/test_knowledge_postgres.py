"""Real PostgreSQL coverage for versioned knowledge ingestion and ACL reads."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.migrate import upgrade_database
from app.knowledge.ingestion import (
    CorpusIngestor,
    KnowledgeIngestionError,
    PreparedChunk,
    PreparedCorpusBuild,
)
from app.knowledge.postgres import KnowledgeSnapshotError, PostgresKnowledgeStore
from app.knowledge.retrieval import (
    DeterministicKnowledgeQueryPlanner,
    HybridKnowledgeRetriever,
    KnowledgeQueryPlan,
)
from app.knowledge.service import KnowledgeService
from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    BookMappingManifest,
    IndexBuildSpec,
    RetrievalPolicy,
    SourceManifest,
    sha256_text,
    stable_evidence_id,
)
from app.shared.budget import ProviderBudgetContext, provider_budget_scope
from app.v2.authorization import (
    AuthorityOverrideError,
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceBinding,
    ResourceNotFoundError,
)
from app.v2.contracts import ConversationMode
from app.v2.registry import KnowledgeRetrieveInput
from tests.v2_postgres_support import disposable_postgres_database

_ROOT = Path(__file__).resolve().parents[1]


class _OpenAIShapedHashingEmbedder:
    def __init__(
        self,
        *,
        fail: bool = False,
        model: str = "text-embedding-3-small",
        dimensions: int = 32,
    ) -> None:
        self.fail = fail
        self.model = model
        self.method = f"openai_{model}_{dimensions}_test"
        self.dimensions = dimensions
        self.calls: list[tuple[str, ...]] = []

    def embed(self, value: str) -> list[float]:
        return self.embed_many([value])[0]

    def embed_many(self, values: list[str]) -> list[list[float]]:
        self.calls.append(tuple(values))
        if self.fail:
            raise RuntimeError("synthetic interruption after provider dispatch")
        vectors: list[list[float]] = []
        for value in values:
            digest = hashlib.sha256(value.encode()).digest()
            repeated = (digest * ((self.dimensions // len(digest)) + 1))[
                : self.dimensions
            ]
            vectors.append([float(item) / 255.0 for item in repeated])
        return vectors


class _BlockingHashingEmbedder(_OpenAIShapedHashingEmbedder):
    def __init__(self, started: Event, release: Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    def embed_many(self, values: list[str]) -> list[list[float]]:
        self._started.set()
        if not self._release.wait(timeout=10):
            raise RuntimeError("synthetic provider release timed out")
        return super().embed_many(values)


class _RecordingQueryEmbedder(_OpenAIShapedHashingEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.method = "test_query_embedding_runtime"


class _RecordingPlanner:
    def __init__(self) -> None:
        self.source_id_calls: list[tuple[str, ...]] = []
        self.inner = DeterministicKnowledgeQueryPlanner()

    async def plan(
        self,
        query: str,
        sources: Sequence[AuthorizedKnowledgeSource],
    ) -> KnowledgeQueryPlan:
        self.source_id_calls.append(tuple(source.source_id for source in sources))
        return await self.inner.plan(query, sources)


class _InterruptBeforeClaimStore(PostgresKnowledgeStore):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interrupt_before_claim: int,
    ) -> None:
        super().__init__(session_factory)
        self.claim_count = 0
        self.interrupt_before_claim = interrupt_before_claim

    def claim_embedding_batch(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        chunks: Sequence[PreparedChunk],
    ) -> str | None:
        self.claim_count += 1
        if self.claim_count == self.interrupt_before_claim:
            raise RuntimeError("synthetic interruption before next claim")
        return super().claim_embedding_batch(
            build,
            build_owner_id=build_owner_id,
            chunks=chunks,
        )


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    with disposable_postgres_database("thanh_v2_p2_knowledge_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO products "
                    "(id, name, category, price, description, platform) "
                    "VALUES (:id, :name, :category, 100000, "
                    "'Synthetic Package 3 catalog row', 'Tiki')"
                ),
                [
                    {
                        "id": product_id,
                        "name": f"Synthetic book {product_id}",
                        "category": (
                            "Sữa tiệt trùng (UHT)"
                            if product_id == 58
                            else (
                                "Tiểu thuyết" if product_id % 2 else "Sách kỹ năng sống"
                            )
                        ),
                    }
                    for product_id in range(1, 201)
                ],
            )
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def postgres_sessions(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )


def _manifests(
    *,
    corpus_version: str,
    content_suffix: str,
) -> tuple[SourceManifest, BookMappingManifest]:
    sources = SourceManifest.model_validate_json(
        (_ROOT / "data/knowledge/books-v1/sources.json").read_text(encoding="utf-8")
    )
    mappings = BookMappingManifest.model_validate_json(
        (_ROOT / "data/knowledge/books-v1/mappings.json").read_text(encoding="utf-8")
    )
    source_payloads: list[dict[str, object]] = []
    for source in sources.sources:
        payload = source.model_dump(mode="json")
        content = f"{source.content_markdown.rstrip()}\n\n{content_suffix}\n"
        payload["content_markdown"] = content
        payload["content_sha256"] = sha256_text(content)
        source_payloads.append(payload)
    source_manifest = SourceManifest.model_validate(
        {
            **sources.model_dump(mode="json", exclude={"sources"}),
            "corpus_version": corpus_version,
            "sources": source_payloads,
        }
    )
    mapping_manifest = BookMappingManifest.model_validate(
        {
            **mappings.model_dump(mode="json", exclude={"mappings"}),
            "corpus_version": corpus_version,
            "mappings": [item.model_dump(mode="json") for item in mappings.mappings],
        }
    )
    return source_manifest, mapping_manifest


def _acl_manifests() -> tuple[SourceManifest, BookMappingManifest]:
    sources, mappings = _manifests(
        corpus_version="postgres-facade-acl-v1",
        content_suffix="Facade ACL searchable phrase.",
    )
    payload = sources.model_dump(mode="json")
    source_payloads = payload["sources"]
    source_payloads[1]["access"] = {
        "allowed_tenant_ids": ["default"],
        "principal_policy": "allowlist",
        "allowed_principal_ids": ["principal_test"],
        "shopper_required_scopes": ["ecommerce.read"],
        "merchant_required_scopes": ["ecommerce.read", "merchant.read"],
    }
    source_payloads[2]["access"] = {
        "allowed_tenant_ids": ["default"],
        "principal_policy": "authenticated",
        "allowed_principal_ids": [],
        "shopper_required_scopes": ["ecommerce.read", "knowledge.premium"],
        "merchant_required_scopes": ["ecommerce.read", "merchant.read"],
    }
    source_payloads[3]["access"] = {
        "allowed_tenant_ids": ["default"],
        "principal_policy": "authenticated",
        "allowed_principal_ids": [],
        "shopper_required_scopes": None,
        "merchant_required_scopes": ["ecommerce.read", "merchant.read"],
    }
    for source_payload in source_payloads[4:]:
        source_payload["access"]["allowed_tenant_ids"] = ["foreign"]
    return SourceManifest.model_validate(payload), mappings


def _policy() -> RetrievalPolicy:
    return RetrievalPolicy(
        version="policy_v1",
        dense_weight=1.0,
        lexical_weight=1.0,
        rrf_k=60,
        candidate_limit=30,
        default_limit=6,
        max_limit=8,
        max_context_tokens=6_000,
        min_dense_relevance=0.2,
        min_lexical_coverage=0.1,
    )


def _ingestor(
    store: PostgresKnowledgeStore,
    embedder: _OpenAIShapedHashingEmbedder,
    *,
    owner: str,
    chunker_version: str = "table_chunker_v1",
    enrichment_policy_version: str = "source_keywords_v1",
    batch_size: int = 8,
) -> CorpusIngestor:
    return CorpusIngestor(
        store=store,
        embedder=embedder,
        index_spec=IndexBuildSpec(
            embedding_model=embedder.model,
            embedding_dimension=embedder.dimensions,
            chunker_version=chunker_version,
            enrichment_policy_version=enrichment_policy_version,
        ),
        retrieval_policy=_policy(),
        embedding_batch_size=batch_size,
        build_owner_id=owner,
    )


def _budget(scope_id: str) -> ProviderBudgetContext:
    return ProviderBudgetContext(
        ledger=object(),  # type: ignore[arg-type]
        scope_id=scope_id,
        purpose="ingestion",
    )


def _access(
    *,
    tenant_id: str = "default",
    principal_id: str = "principal_test",
    scopes: frozenset[str] = frozenset({"ecommerce.read"}),
    mode: ConversationMode = ConversationMode.SHOPPER,
) -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode=mode,
            store_id="demo",
        ),
        scopes=scopes,
    )


def test_publish_resolve_and_reopen_apply_current_sql_authorization(
    postgres_sessions: sessionmaker[Session],
) -> None:
    sources, mappings = _manifests(
        corpus_version="postgres-v1",
        content_suffix="PostgreSQL publication fixture.",
    )
    store = PostgresKnowledgeStore(postgres_sessions)
    embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_publish_scope")):
        result = _ingestor(store, embedder, owner="p3_pg_publish_owner").ingest(
            sources, mappings, publish=True
        )

    assert result.published is True
    assert result.source_count == 20
    assert result.mapping_count == 200
    snapshot = store.resolve_published_snapshot(result.corpus_version_id)
    authorized_sources = store.list_authorized_sources(snapshot, _access())
    assert len(authorized_sources) == 20

    exact_mapping = next(mapping for mapping in mappings.mappings if mapping.source_ids)
    mapped_sources = store.list_authorized_sources(
        snapshot,
        _access(),
        product_ids=(exact_mapping.product_id,),
    )
    assert {source.source_id for source in mapped_sources} == set(
        exact_mapping.source_ids
    )
    chunks = store.load_authorized_chunks(
        snapshot,
        _access(),
        source_ids=frozenset({mapped_sources[0].source_id}),
    )
    assert chunks
    chunk = chunks[0]
    evidence = store.resolve_authorized_evidence(
        snapshot,
        _access(),
        source_id=chunk.source_id,
        source_version_id=chunk.source_version_id,
        chunk_id=chunk.chunk_id,
        span_id=chunk.spans[0].span_id,
    )
    assert evidence.excerpt == chunk.content
    assert evidence.observed_at == mapped_sources[0].retrieved_at
    assert evidence.title == mapped_sources[0].title
    assert evidence.url == mapped_sources[0].url
    assert evidence.evidence_id == stable_evidence_id(
        source_id=chunk.source_id,
        source_version_id=chunk.source_version_id,
        chunk_id=chunk.chunk_id,
        span_id=chunk.spans[0].span_id,
    )

    assert store.list_authorized_sources(snapshot, _access(tenant_id="foreign")) == ()
    with pytest.raises(AuthorityOverrideError):
        store.load_authorized_chunks(
            snapshot,
            _access(tenant_id="foreign"),
            source_ids=frozenset({chunk.source_id}),
        )
    with pytest.raises(AuthorizationDeniedError):
        store.resolve_authorized_evidence(
            snapshot,
            _access(scopes=frozenset()),
            source_id=chunk.source_id,
            source_version_id=chunk.source_version_id,
            chunk_id=chunk.chunk_id,
            span_id=chunk.spans[0].span_id,
        )

    copied_sources, copied_mappings = _manifests(
        corpus_version="postgres-v2",
        content_suffix="PostgreSQL publication fixture.",
    )
    copied_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_copy_scope")):
        copied = _ingestor(
            store,
            copied_embedder,
            owner="p3_pg_copy_owner",
        ).ingest(copied_sources, copied_mappings, publish=True)
    assert copied.reused_vector_count == copied.vector_count
    assert copied_embedder.calls == []
    assert store.resolve_published_snapshot(copied.corpus_version_id)

    noop_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_noop_scope")):
        noop = _ingestor(
            store,
            noop_embedder,
            owner="p3_pg_different_noop_owner",
        ).ingest(sources, mappings, publish=True)
    assert noop.published is True
    assert noop.reused_vector_count == noop.vector_count
    assert noop_embedder.calls == []


@pytest.mark.asyncio
async def test_real_facade_filters_acl_before_planning_and_reopens_exact_evidence(
    postgres_sessions: sessionmaker[Session],
) -> None:
    sources, mappings = _acl_manifests()
    store = PostgresKnowledgeStore(postgres_sessions)
    ingestion_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_facade_acl_ingestion_scope")):
        ingested = _ingestor(
            store,
            ingestion_embedder,
            owner="p3_pg_facade_acl_ingestion_owner",
        ).ingest(sources, mappings, publish=True)

    source_ids = tuple(source.source_id for source in sources.sources)
    expected_shopper_ids = frozenset(source_ids[:3])
    main_access = _access(scopes=frozenset({"ecommerce.read", "knowledge.premium"}))
    query_embedder = _RecordingQueryEmbedder()
    planner = _RecordingPlanner()
    service = KnowledgeService(
        HybridKnowledgeRetriever(store, query_embedder, planner=planner)
    )
    corpus_id = ingested.corpus_version_id
    index_id = ingested.index_manifest_id

    broad = await service.retrieve(
        KnowledgeRetrieveInput(query="Facade ACL searchable phrase", top_k=8),
        main_access,
        corpus_version_id=corpus_id,
        index_manifest_id=index_id,
    )
    assert frozenset(planner.source_id_calls[0]) == expected_shopper_ids
    assert frozenset(broad.retrieval.plan.source_ids) == expected_shopper_ids
    diagnostic_source_ids = frozenset(
        diagnostic.source_id for diagnostic in broad.retrieval.diagnostics
    )
    assert diagnostic_source_ids
    assert diagnostic_source_ids <= expected_shopper_ids
    assert broad.evidence
    assert all(item.source_id in expected_shopper_ids for item in broad.evidence)

    allowlisted = sources.sources[1]
    allowlisted_result = await service.retrieve(
        KnowledgeRetrieveInput(
            query=allowlisted.title,
            requested_source_ids=frozenset({allowlisted.source_id}),
            top_k=1,
        ),
        main_access,
        corpus_version_id=corpus_id,
        index_manifest_id=index_id,
    )
    reference = allowlisted_result.evidence[0]
    assert reference.source_id == allowlisted.source_id
    assert reference.chunk_id is not None
    assert reference.span_id is not None
    reopened = await service.reopen_evidence(
        reference,
        main_access,
        corpus_version_id=corpus_id,
        index_manifest_id=index_id,
    )
    assert reopened.evidence_id == reference.evidence_id
    assert reopened.excerpt == allowlisted_result.result.excerpts[0].excerpt
    assert reopened.observed_at == reference.observed_at == allowlisted.retrieved_at
    assert reopened.title == reference.title
    assert str(reopened.url) == str(reference.url)

    with pytest.raises(ResourceNotFoundError):
        await service.reopen_evidence(
            reference,
            _access(
                principal_id="another_principal",
                scopes=frozenset({"ecommerce.read", "knowledge.premium"}),
            ),
            corpus_version_id=corpus_id,
            index_manifest_id=index_id,
        )

    premium = sources.sources[2]
    premium_result = await service.retrieve(
        KnowledgeRetrieveInput(
            query=premium.title,
            requested_source_ids=frozenset({premium.source_id}),
            top_k=1,
        ),
        main_access,
        corpus_version_id=corpus_id,
        index_manifest_id=index_id,
    )
    premium_reference = premium_result.evidence[0]
    assert premium_reference.span_id is not None
    with pytest.raises(ResourceNotFoundError):
        await service.reopen_evidence(
            premium_reference,
            _access(scopes=frozenset({"ecommerce.read"})),
            corpus_version_id=corpus_id,
            index_manifest_id=index_id,
        )

    wrong_version = sources.sources[0].source_version_id
    wrong_version_reference = reference.model_copy(
        update={
            "source_version_id": wrong_version,
            "evidence_id": stable_evidence_id(
                source_id=reference.source_id,
                source_version_id=wrong_version,
                chunk_id=reference.chunk_id,
                span_id=reference.span_id,
            ),
        }
    )
    with pytest.raises(ResourceNotFoundError):
        await service.reopen_evidence(
            wrong_version_reference,
            main_access,
            corpus_version_id=corpus_id,
            index_manifest_id=index_id,
        )

    wrong_span = premium_reference.span_id
    wrong_span_reference = reference.model_copy(
        update={
            "span_id": wrong_span,
            "evidence_id": stable_evidence_id(
                source_id=reference.source_id,
                source_version_id=reference.source_version_id,
                chunk_id=reference.chunk_id,
                span_id=wrong_span,
            ),
        }
    )
    with pytest.raises(ResourceNotFoundError):
        await service.reopen_evidence(
            wrong_span_reference,
            main_access,
            corpus_version_id=corpus_id,
            index_manifest_id=index_id,
        )

    merchant_result = await service.retrieve(
        KnowledgeRetrieveInput(
            query=sources.sources[3].title,
            requested_source_ids=frozenset({source_ids[3]}),
            top_k=1,
        ),
        _access(
            scopes=frozenset({"ecommerce.read", "merchant.read"}),
            mode=ConversationMode.MERCHANT,
        ),
        corpus_version_id=corpus_id,
        index_manifest_id=index_id,
    )
    assert merchant_result.evidence[0].source_id == source_ids[3]
    assert {
        source.source_id
        for source in store.list_authorized_sources(
            broad.retrieval.snapshot,
            _access(
                tenant_id="foreign",
                scopes=frozenset({"ecommerce.read", "knowledge.premium"}),
            ),
        )
    } == set(source_ids[4:])

    planner_call_count = len(planner.source_id_calls)
    query_call_count = len(query_embedder.calls)
    empty = await service.retrieve(
        KnowledgeRetrieveInput(query="no authorized sources", top_k=1),
        _access(tenant_id="empty"),
        corpus_version_id=corpus_id,
        index_manifest_id=index_id,
    )
    assert empty.result.answerable is False
    assert empty.evidence == ()
    assert len(planner.source_id_calls) == planner_call_count
    assert len(query_embedder.calls) == query_call_count


def test_completed_batches_resume_under_new_budget_scope_without_duplicate_calls(
    postgres_sessions: sessionmaker[Session],
) -> None:
    sources, mappings = _manifests(
        corpus_version="postgres-restart-v1",
        content_suffix="Restart-safe unique fixture.",
    )
    interrupted_store = _InterruptBeforeClaimStore(
        postgres_sessions,
        interrupt_before_claim=2,
    )
    first_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_restart_scope_one")):
        with pytest.raises(RuntimeError, match="before next claim"):
            _ingestor(
                interrupted_store,
                first_embedder,
                owner="stable_restart_owner",
                batch_size=3,
            ).ingest(sources, mappings)

    with postgres_sessions() as session:
        persisted_count = session.scalar(
            text(
                "SELECT count(*) FROM v2_knowledge_vectors AS vector "
                "JOIN v2_knowledge_index_manifests AS index_manifest "
                "ON index_manifest.id = vector.index_manifest_id "
                "AND index_manifest.corpus_version_id = vector.corpus_version_id "
                "JOIN v2_knowledge_corpus_versions AS corpus "
                "ON corpus.id = index_manifest.corpus_version_id "
                "WHERE corpus.corpus_name = :corpus_name "
                "AND corpus.version = :corpus_version "
                "AND index_manifest.embedding_model = :embedding_model "
                "AND index_manifest.embedding_dimension = :embedding_dimension "
                "AND index_manifest.chunker_version = 'table_chunker_v1' "
                "AND index_manifest.enrichment_policy_version "
                "= 'source_keywords_v1'"
            ),
            {
                "corpus_name": sources.corpus_name,
                "corpus_version": sources.corpus_version,
                "embedding_model": first_embedder.model,
                "embedding_dimension": first_embedder.dimensions,
            },
        )
    assert len(first_embedder.calls) == 1
    assert len(first_embedder.calls[0]) == 3
    assert persisted_count == 3

    second_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_restart_scope_two")):
        result = _ingestor(
            PostgresKnowledgeStore(postgres_sessions),
            second_embedder,
            owner="stable_restart_owner",
            batch_size=3,
        ).ingest(sources, mappings)
    assert result.reused_vector_count >= 3
    assert sum(len(call) for call in second_embedder.calls) == (
        result.vector_count - result.reused_vector_count
    )


def test_unpersisted_claim_is_fail_closed_and_identity_conflict_precedes_provider(
    postgres_sessions: sessionmaker[Session],
) -> None:
    sources, mappings = _manifests(
        corpus_version="postgres-unknown-v1",
        content_suffix="Unknown outcome unique fixture.",
    )
    store = PostgresKnowledgeStore(postgres_sessions)
    failing_embedder = _OpenAIShapedHashingEmbedder(fail=True)
    with provider_budget_scope(_budget("p3_pg_unknown_scope_one")):
        with pytest.raises(RuntimeError, match="after provider dispatch"):
            _ingestor(store, failing_embedder, owner="unknown_owner").ingest(
                sources, mappings
            )

    retry_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_unknown_scope_two")):
        with pytest.raises(KnowledgeIngestionError, match="outcome_unknown"):
            _ingestor(store, retry_embedder, owner="unknown_owner").ingest(
                sources, mappings
            )
    assert retry_embedder.calls == []

    changed_payload = sources.model_dump(mode="json")
    changed_payload["sources"][0]["title"] = "Changed immutable title"
    changed_sources = SourceManifest.model_validate(changed_payload)
    conflict_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_conflict_scope")):
        with pytest.raises(
            KnowledgeIngestionError, match="immutable identity conflict"
        ):
            _ingestor(store, conflict_embedder, owner="other_owner").ingest(
                changed_sources, mappings
            )
    assert conflict_embedder.calls == []


def test_changed_index_fingerprints_persist_fresh_vectors_and_preserve_old_evidence(
    postgres_sessions: sessionmaker[Session],
) -> None:
    sources, mappings = _manifests(
        corpus_version="postgres-fingerprint-v1",
        content_suffix="Fingerprint unique fixture.",
    )
    store = PostgresKnowledgeStore(postgres_sessions)
    first_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_fingerprint_scope_one")):
        first = _ingestor(
            store,
            first_embedder,
            owner="fingerprint_owner_one",
            chunker_version="table_chunker_v1",
        ).ingest(sources, mappings, publish=True)
    first_snapshot = store.resolve_published_snapshot(
        first.corpus_version_id,
        index_manifest_id=first.index_manifest_id,
    )
    first_source = store.list_authorized_sources(first_snapshot, _access())[0]
    first_chunk = store.load_authorized_chunks(
        first_snapshot,
        _access(),
        source_ids=frozenset({first_source.source_id}),
    )[0]

    enrichment_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_fingerprint_enrichment_scope")):
        enrichment_index = _ingestor(
            store,
            enrichment_embedder,
            owner="fingerprint_enrichment_owner",
            enrichment_policy_version="none_v1",
        ).ingest(sources, mappings, publish=True)
    assert first.index_fingerprint != enrichment_index.index_fingerprint
    assert enrichment_index.reused_vector_count == 0
    assert sum(len(call) for call in enrichment_embedder.calls) == (
        enrichment_index.vector_count
    )

    model_embedder = _OpenAIShapedHashingEmbedder(
        model="text-embedding-3-large",
        dimensions=64,
    )
    with provider_budget_scope(_budget("p3_pg_fingerprint_model_scope")):
        model_index = _ingestor(
            store,
            model_embedder,
            owner="fingerprint_model_owner",
        ).ingest(sources, mappings, publish=True)
    assert first.index_fingerprint != model_index.index_fingerprint
    assert model_index.reused_vector_count == 0
    assert sum(len(call) for call in model_embedder.calls) == model_index.vector_count

    chunker_embedder = _OpenAIShapedHashingEmbedder()
    with provider_budget_scope(_budget("p3_pg_fingerprint_chunker_scope")):
        chunker_index = _ingestor(
            store,
            chunker_embedder,
            owner="fingerprint_chunker_owner",
            chunker_version="table_chunker_v2",
        ).ingest(sources, mappings, publish=True)

    assert first.index_fingerprint != chunker_index.index_fingerprint
    assert chunker_index.reused_vector_count == 0
    assert sum(len(call) for call in chunker_embedder.calls) == (
        chunker_index.vector_count
    )
    with pytest.raises(KnowledgeSnapshotError, match="index manifest is required"):
        store.resolve_published_snapshot(chunker_index.corpus_version_id)
    explicit = store.resolve_published_snapshot(
        chunker_index.corpus_version_id,
        index_manifest_id=chunker_index.index_manifest_id,
    )
    assert explicit.index_fingerprint == chunker_index.index_fingerprint
    preserved = store.resolve_authorized_evidence(
        first_snapshot,
        _access(),
        source_id=first_chunk.source_id,
        source_version_id=first_chunk.source_version_id,
        chunk_id=first_chunk.chunk_id,
        span_id=first_chunk.spans[0].span_id,
    )
    assert preserved.excerpt == first_chunk.content


def test_concurrent_build_cannot_dispatch_the_same_missing_batch(
    postgres_sessions: sessionmaker[Session],
) -> None:
    sources, mappings = _manifests(
        corpus_version="postgres-concurrent-v1",
        content_suffix="Concurrent ownership unique fixture.",
    )
    store = PostgresKnowledgeStore(postgres_sessions)
    started = Event()
    release = Event()
    first_embedder = _BlockingHashingEmbedder(started, release)

    def run_first() -> None:
        with provider_budget_scope(_budget("p3_pg_concurrent_scope_one")):
            _ingestor(
                store,
                first_embedder,
                owner="concurrent_owner_one",
            ).ingest(sources, mappings)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(run_first)
        assert started.wait(timeout=10)
        second_embedder = _OpenAIShapedHashingEmbedder()
        try:
            with provider_budget_scope(_budget("p3_pg_concurrent_scope_two")):
                with pytest.raises(
                    KnowledgeIngestionError,
                    match="outcome_unknown|owned_by_another_run",
                ):
                    _ingestor(
                        store,
                        second_embedder,
                        owner="concurrent_owner_two",
                    ).ingest(sources, mappings)
        finally:
            release.set()
        first.result(timeout=10)
    assert second_embedder.calls == []
