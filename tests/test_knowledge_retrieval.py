"""Hybrid ranking, authorization order, bounds, and failure regressions."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.knowledge.retrieval import (
    DeterministicKnowledgeQueryPlanner,
    HybridKnowledgeRetriever,
    KnowledgePlanningError,
    KnowledgeQueryPlan,
    KnowledgeSnapshotError,
    ModelRuntimeKnowledgeQueryPlanner,
)
from app.knowledge.service import KnowledgeService
from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    IndexBuildSpec,
    KnowledgeSpan,
    PublishedKnowledgeSnapshot,
    RetrievalChunk,
    RetrievalPolicy,
    SourceSupportScope,
    content_addressed_id,
    sha256_text,
    sha256_utf8,
    stable_chunk_id,
    stable_span_id,
)
from app.shared.budget import (
    BudgetCancelledError,
    BudgetLimitExceededError,
    ProviderBudgetContext,
    conservative_text_token_bound,
    current_provider_budget,
    provider_budget_scope,
)
from app.shared.model_runtime import ModelCallMetadata, ModelRuntimeError
from app.v2.authorization import (
    AuthorityOverrideError,
    ResourceAuthorization,
    ResourceBinding,
)
from app.v2.contracts import ConversationMode
from app.v2.registry import KnowledgeRetrieveInput

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _vector(axis: int, *, value: float = 1.0) -> tuple[float, ...]:
    return tuple(value if index == axis else 0.0 for index in range(32))


class StaticEmbedder:
    dimensions = 32
    model = "test-embedding-v1"
    method = "test-embedding-v1"

    def __init__(self, vector: tuple[float, ...] | None = None) -> None:
        self.vector = vector or _vector(0)
        self.calls: list[list[str]] = []
        self.thread_ids: list[int] = []

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        self.thread_ids.append(threading.get_ident())
        return [list(self.vector) for _ in texts]


def _policy(**updates: object) -> RetrievalPolicy:
    payload: dict[str, object] = {
        "version": "hybrid_v1",
        "dense_weight": 1.0,
        "lexical_weight": 1.0,
        "rrf_k": 60,
        "candidate_limit": 30,
        "default_limit": 6,
        "max_limit": 8,
        "max_context_tokens": 6_000,
        "min_dense_relevance": 0.8,
        "min_lexical_coverage": 0.5,
    }
    payload.update(updates)
    return RetrievalPolicy.model_validate(payload)


def _snapshot(
    *,
    embedder: StaticEmbedder | None = None,
    policy: RetrievalPolicy | None = None,
) -> PublishedKnowledgeSnapshot:
    runtime = embedder or StaticEmbedder()
    spec = IndexBuildSpec(
        embedding_model=runtime.model,
        embedding_dimension=runtime.dimensions,
        chunker_version="table_chunker_v1",
        enrichment_policy_version="source_enrichment_v1",
    )
    return PublishedKnowledgeSnapshot(
        corpus_version_id=content_addressed_id("cor", {"corpus": "test"}),
        corpus_name="books-v1",
        corpus_version="2026-09-09",
        index_manifest_id=content_addressed_id("idx", {"index": "test"}),
        index_fingerprint=spec.index_fingerprint,
        embedding_model=spec.embedding_model,
        embedding_dimension=spec.embedding_dimension,
        chunker_version=spec.chunker_version,
        enrichment_policy_version=spec.enrichment_policy_version,
        query_embedding_fingerprint=spec.query_embedding_fingerprint,
        retrieval_policy=policy or _policy(),
        published_at=NOW,
    )


def _source(index: int, **updates: object) -> AuthorizedKnowledgeSource:
    payload: dict[str, object] = {
        "source_id": f"src_book_{index:03d}",
        "source_version_id": content_addressed_id("svr", {"source": index}),
        "title": f"Book {index}",
        "url": f"https://example.test/books/{index}",
        "planning_text": f"Public information for work {index}",
        "keywords": (f"keyword-{index}",),
        "support_scope": SourceSupportScope.WORK,
        "work_identifier": f"work_book_{index}",
        "edition_identifier": None,
        "retrieved_at": NOW,
    }
    payload.update(updates)
    return AuthorizedKnowledgeSource.model_validate(payload)


def _chunk(
    snapshot: PublishedKnowledgeSnapshot,
    source: AuthorizedKnowledgeSource,
    *,
    content: str,
    vector: tuple[float, ...],
    chunk_index: int = 0,
    token_count: int = 10,
) -> RetrievalChunk:
    content_hash = sha256_text(content)
    chunk_id = stable_chunk_id(
        source_version_id=source.source_version_id,
        chunker_version=snapshot.chunker_version,
        chunk_index=chunk_index,
        content_hash=content_hash,
    )
    span_hash = sha256_utf8(content)
    span = KnowledgeSpan(
        span_id=stable_span_id(
            chunk_id=chunk_id,
            start_char=0,
            end_char=len(content),
            content_hash=span_hash,
        ),
        start_char=0,
        end_char=len(content),
        content_hash=span_hash,
    )
    return RetrievalChunk(
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
        source_id=source.source_id,
        source_version_id=source.source_version_id,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        chunker_version=snapshot.chunker_version,
        title=source.title,
        url=source.url,
        content=content,
        token_count=token_count,
        content_hash=content_hash,
        vector=vector,
        spans=(span,),
        support_scope=source.support_scope,
        work_identifier=source.work_identifier,
        edition_identifier=source.edition_identifier,
    )


def _access() -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="default",
            principal_id="retrieval-test-user",
            mode=ConversationMode.SHOPPER,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read"}),
    )


class FakeStore:
    def __init__(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        sources: tuple[AuthorizedKnowledgeSource, ...],
        chunks: tuple[RetrievalChunk, ...],
        *,
        events: list[str] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.sources = sources
        self.chunks = chunks
        self.events = events if events is not None else []
        self.thread_ids: list[int] = []
        self.loaded_source_ids: frozenset[str] | None = None

    def _record(self, event: str) -> None:
        self.events.append(event)
        self.thread_ids.append(threading.get_ident())

    def resolve_published_snapshot(
        self, corpus_version_id: str, *, index_manifest_id: str | None = None
    ) -> PublishedKnowledgeSnapshot:
        del corpus_version_id, index_manifest_id
        self._record("snapshot")
        return self.snapshot

    def list_authorized_sources(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        product_ids: tuple[int, ...] = (),
    ) -> tuple[AuthorizedKnowledgeSource, ...]:
        del snapshot, access, product_ids
        self._record("sources")
        return self.sources

    def load_authorized_chunks(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_ids: frozenset[str],
    ) -> tuple[RetrievalChunk, ...]:
        del snapshot, access
        self._record("chunks")
        self.loaded_source_ids = source_ids
        return tuple(chunk for chunk in self.chunks if chunk.source_id in source_ids)

    def resolve_authorized_evidence(self, *_: object, **__: object) -> Any:
        raise AssertionError("evidence reopening is outside this test")


class RecordingPlanner:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.inner = DeterministicKnowledgeQueryPlanner()

    async def plan(
        self, query: str, sources: tuple[AuthorizedKnowledgeSource, ...]
    ) -> KnowledgeQueryPlan:
        self.events.append("planner")
        return await self.inner.plan(query, sources)


@pytest.mark.asyncio
async def test_acl_reads_precede_planning_and_store_reads_leave_event_loop() -> None:
    embedder = StaticEmbedder()
    snapshot = _snapshot(embedder=embedder)
    source = _source(1, planning_text="orbital astronomy")
    chunk = _chunk(
        snapshot,
        source,
        content="Orbital astronomy explains the observed motion.",
        vector=_vector(0),
    )
    events: list[str] = []
    store = FakeStore(snapshot, (source,), (chunk,), events=events)
    retriever = HybridKnowledgeRetriever(
        store,
        embedder,
        planner=RecordingPlanner(events),
    )
    loop_thread = threading.get_ident()

    bundle = await retriever.retrieve(
        "orbital astronomy",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
        limit=1,
    )

    assert events == ["snapshot", "sources", "planner", "chunks"]
    assert store.thread_ids and all(value != loop_thread for value in store.thread_ids)
    assert embedder.thread_ids and embedder.thread_ids[0] != loop_thread
    assert bundle.answerable


@pytest.mark.asyncio
async def test_deterministic_planner_preserves_dense_recall_across_twenty_sources() -> (
    None
):
    embedder = StaticEmbedder(_vector(0))
    snapshot = _snapshot(embedder=embedder)
    sources = tuple(_source(index) for index in range(1, 21))
    target = sources[-1]
    chunks = tuple(
        _chunk(
            snapshot,
            source,
            content=f"Cross-language factual note {index}.",
            vector=_vector(0) if source == target else _vector(1),
        )
        for index, source in enumerate(sources, start=1)
    )
    store = FakeStore(snapshot, sources, chunks)

    bundle = await HybridKnowledgeRetriever(store, embedder).retrieve(
        "hoàn toàn khác ngôn ngữ",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
        limit=1,
    )

    assert store.loaded_source_ids == frozenset(source.source_id for source in sources)
    assert bundle.hits[0].chunk.source_id == target.source_id
    assert len(bundle.diagnostics) == 20


@pytest.mark.asyncio
async def test_weighted_rrf_keeps_raw_channel_provenance() -> None:
    embedder = StaticEmbedder(_vector(0))
    policy = _policy(dense_weight=2.0, lexical_weight=1.0)
    snapshot = _snapshot(embedder=embedder, policy=policy)
    source = _source(1)
    chunk = _chunk(
        snapshot,
        source,
        content="astronomy orbit astronomy",
        vector=_vector(0),
    )
    store = FakeStore(snapshot, (source,), (chunk,))

    bundle = await HybridKnowledgeRetriever(store, embedder).retrieve(
        "astronomy orbit",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
        limit=1,
    )

    trace = bundle.trace[0]
    assert trace.channels == ("bm25", "dense")
    assert trace.lexical_score > 0
    assert trace.dense_score == pytest.approx(1.0)
    assert trace.rrf_score == pytest.approx(3.0 / 61.0)
    diagnostic = bundle.diagnostics[0]
    assert diagnostic.passes_dense_threshold
    assert diagnostic.passes_lexical_threshold
    assert diagnostic.disposition == "selected"


@pytest.mark.asyncio
async def test_low_raw_relevance_abstains_with_visible_threshold_diagnostics() -> None:
    embedder = StaticEmbedder(_vector(0))
    snapshot = _snapshot(embedder=embedder)
    sources = (_source(1), _source(2))
    chunks = tuple(
        _chunk(
            snapshot,
            source,
            content=f"Edition disclaimer and publication note {index}.",
            vector=_vector(1),
        )
        for index, source in enumerate(sources, start=1)
    )
    bundle = await HybridKnowledgeRetriever(
        FakeStore(snapshot, sources, chunks), embedder
    ).retrieve(
        "quantum weather on mars",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )

    assert not bundle.answerable
    assert bundle.hits == ()
    assert bundle.trace == ()
    assert {item.disposition for item in bundle.diagnostics} == {
        "below_relevance_threshold"
    }
    assert all(not item.passes_dense_threshold for item in bundle.diagnostics)
    assert all(not item.passes_lexical_threshold for item in bundle.diagnostics)


@pytest.mark.asyncio
async def test_diagnostics_distinguish_candidate_exclusion_from_threshold_failure() -> (
    None
):
    embedder = StaticEmbedder(_vector(0))
    snapshot = _snapshot(
        embedder=embedder,
        policy=_policy(candidate_limit=8, min_dense_relevance=0.9),
    )
    sources = tuple(_source(index) for index in range(1, 11))
    chunks = tuple(
        _chunk(
            snapshot,
            source,
            content=f"Unrelated note {index}.",
            vector=_vector(1),
        )
        for index, source in enumerate(sources, start=1)
    )

    bundle = await HybridKnowledgeRetriever(
        FakeStore(snapshot, sources, chunks), embedder
    ).retrieve(
        "no matching vocabulary",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )

    dispositions = [item.disposition for item in bundle.diagnostics]
    assert dispositions.count("below_relevance_threshold") == 8
    assert dispositions.count("outside_candidate_union") == 2
    assert sorted(item.dense_rank for item in bundle.diagnostics) == list(range(1, 11))


@pytest.mark.asyncio
async def test_context_uses_safe_unicode_bound_and_ignores_tampered_word_counts() -> (
    None
):
    embedder = StaticEmbedder(_vector(0))
    snapshot = _snapshot(embedder=embedder)
    sources = tuple(_source(index) for index in range(1, 4))
    chunks = tuple(
        _chunk(
            snapshot,
            source,
            content=f"needle-{index} " + ("ắ" * 1_400),
            vector=_vector(0),
            token_count=1,
        )
        for index, source in enumerate(sources, start=1)
    )

    bundle = await HybridKnowledgeRetriever(
        FakeStore(snapshot, sources, chunks), embedder
    ).retrieve(
        "needle",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )

    assert len(bundle.hits) == 1
    assert bundle.context_token_count == conservative_text_token_bound(bundle.context)
    assert bundle.context_token_count <= 6_000
    assert len(bundle.context.encode("utf-8")) <= 6_000


@pytest.mark.asyncio
async def test_adjacent_expansion_is_deduplicated_and_included_in_max_eight() -> None:
    embedder = StaticEmbedder(_vector(0))
    snapshot = _snapshot(embedder=embedder)
    source = _source(1)
    chunks = tuple(
        _chunk(
            snapshot,
            source,
            content=f"section {index} " + ("target" if index % 2 == 0 else "context"),
            vector=_vector(0) if index % 2 == 0 else _vector(1),
            chunk_index=index,
        )
        for index in range(10)
    )

    bundle = await HybridKnowledgeRetriever(
        FakeStore(snapshot, (source,), chunks), embedder
    ).retrieve(
        "target",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )

    assert len(bundle.hits) == 8
    assert any(hit.channels == ("adjacent",) for hit in bundle.hits)
    assert len({hit.chunk.content_hash for hit in bundle.hits}) == len(bundle.hits)
    assert bundle.context_token_count <= 6_000


@pytest.mark.asyncio
async def test_snapshot_and_query_embedder_are_validated_before_planner_dispatch() -> (
    None
):
    embedder = StaticEmbedder()
    snapshot = _snapshot(embedder=embedder).model_copy(
        update={"index_fingerprint": "0" * 64}
    )
    source = _source(1)
    store = FakeStore(snapshot, (source,), ())
    events: list[str] = []

    with pytest.raises(KnowledgeSnapshotError, match="index_fingerprint"):
        await HybridKnowledgeRetriever(
            store,
            embedder,
            planner=RecordingPlanner(events),
        ).retrieve(
            "query",
            _access(),
            corpus_version_id=snapshot.corpus_version_id,
        )

    assert store.events == ["snapshot"]
    assert events == []
    assert embedder.calls == []


@pytest.mark.asyncio
async def test_requested_source_widening_fails_before_planner_or_provider() -> None:
    embedder = StaticEmbedder()
    snapshot = _snapshot(embedder=embedder)
    source = _source(1)
    events: list[str] = []
    store = FakeStore(snapshot, (source,), (), events=events)

    with pytest.raises(AuthorityOverrideError):
        await HybridKnowledgeRetriever(
            store,
            embedder,
            planner=RecordingPlanner(events),
        ).retrieve(
            "query",
            _access(),
            corpus_version_id=snapshot.corpus_version_id,
            requested_source_ids=frozenset({_source(2).source_id}),
        )

    assert events == ["snapshot", "sources"]
    assert store.loaded_source_ids is None
    assert embedder.calls == []


@pytest.mark.asyncio
async def test_stable_cosine_handles_huge_finite_components() -> None:
    huge = tuple(1e308 for _ in range(32))
    embedder = StaticEmbedder(huge)
    snapshot = _snapshot(embedder=embedder)
    source = _source(1)
    chunk = _chunk(
        snapshot,
        source,
        content="astronomy",
        vector=huge,
    )

    bundle = await HybridKnowledgeRetriever(
        FakeStore(snapshot, (source,), (chunk,)), embedder
    ).retrieve(
        "astronomy",
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
        limit=1,
    )

    assert math.isfinite(bundle.hits[0].dense_score)
    assert bundle.hits[0].dense_score == pytest.approx(1.0)


class FakePlanningRuntime:
    def __init__(self, source_ids: tuple[str, ...]) -> None:
        self.source_ids = source_ids
        self.payload: dict[str, Any] | None = None

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.payload = json.loads(kwargs["input_text"])
        schema = kwargs["schema"]
        return SimpleNamespace(
            value=schema(queries=("rewritten query",), source_ids=self.source_ids)
        )


class RaisingPlanningRuntime:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def generate_structured(self, **_: Any) -> Any:
        raise self.error


def _model_runtime_error() -> ModelRuntimeError:
    metadata = ModelCallMetadata(
        call_id="mcall_" + "0" * 32,
        stage="knowledge_query_plan",
        agent_id="knowledge",
        model="gpt-5.4-mini",
        status="failed",
        duration_ms=1.0,
        attempts=1,
        error_code="model_timeout",
    )
    return ModelRuntimeError("model_timeout", metadata)


def _budget_context(purpose: str = "chat") -> ProviderBudgetContext:
    return ProviderBudgetContext(
        ledger=SimpleNamespace(),
        scope_id=f"knowledge_{purpose}_test",
        purpose=purpose,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_model_catalog_includes_all_small_corpus_sources() -> None:
    sources = tuple(_source(index) for index in range(1, 21))
    runtime = FakePlanningRuntime((sources[-1].source_id,))
    planner = ModelRuntimeKnowledgeQueryPlanner(runtime)  # type: ignore[arg-type]

    with provider_budget_scope(_budget_context()):
        plan = await planner.plan("cross-language question", sources)

    assert plan.source_ids == (sources[-1].source_id,)
    assert runtime.payload is not None
    assert len(runtime.payload["authorized_sources"]) == 20


@pytest.mark.asyncio
async def test_required_model_planner_rejects_out_of_allowlist_decision() -> None:
    sources = (_source(1), _source(2))
    runtime = FakePlanningRuntime(("src_not_authorized",))
    planner = ModelRuntimeKnowledgeQueryPlanner(
        runtime,  # type: ignore[arg-type]
        fallback_on_model_error=False,
    )

    with provider_budget_scope(_budget_context()):
        with pytest.raises(KnowledgePlanningError, match="outside_allowlist"):
            await planner.plan("question", sources)


@pytest.mark.asyncio
async def test_model_runtime_error_falls_back_only_in_hybrid_mode() -> None:
    sources = (_source(1), _source(2))
    hybrid = ModelRuntimeKnowledgeQueryPlanner(
        RaisingPlanningRuntime(_model_runtime_error()),  # type: ignore[arg-type]
    )
    required = ModelRuntimeKnowledgeQueryPlanner(
        RaisingPlanningRuntime(_model_runtime_error()),  # type: ignore[arg-type]
        fallback_on_model_error=False,
    )

    with provider_budget_scope(_budget_context()):
        fallback = await hybrid.plan("question", sources)
        with pytest.raises(ModelRuntimeError, match="model_timeout"):
            await required.plan("question", sources)

    assert fallback.planner == "deterministic_fallback"
    assert fallback.fallback_reason == "model_runtime_error"
    assert set(fallback.source_ids) == {source.source_id for source in sources}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        BudgetLimitExceededError("budget_limit"),
        BudgetCancelledError("provider_dispatch_cancelled"),
        asyncio.CancelledError(),
    ),
)
async def test_budget_and_cancellation_failures_never_become_fallback(
    error: BaseException,
) -> None:
    planner = ModelRuntimeKnowledgeQueryPlanner(
        RaisingPlanningRuntime(error),  # type: ignore[arg-type]
        fallback_on_model_error=True,
    )

    with provider_budget_scope(_budget_context()):
        with pytest.raises(type(error)):
            await planner.plan("question", (_source(1),))


class BlockingProviderEmbedder(StaticEmbedder):
    model = "provider-test-v1"
    method = "openai_provider-test-v1"

    def __init__(self) -> None:
        super().__init__(_vector(0))
        self.started = threading.Event()
        self.stopped = threading.Event()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        del texts
        budget = current_provider_budget()
        assert budget is not None
        self.started.set()
        while not budget.cancelled():
            time.sleep(0.005)
        self.stopped.set()
        raise BudgetCancelledError("provider_dispatch_cancelled")


@pytest.mark.asyncio
async def test_async_cancellation_stops_and_drains_embedding_worker() -> None:
    embedder = BlockingProviderEmbedder()
    snapshot = _snapshot(embedder=embedder)
    source = _source(1)
    chunk = _chunk(
        snapshot,
        source,
        content="cancel target",
        vector=_vector(0),
    )
    service = KnowledgeService(
        HybridKnowledgeRetriever(FakeStore(snapshot, (source,), (chunk,)), embedder)
    )

    with provider_budget_scope(_budget_context("embedding")):
        task = asyncio.create_task(
            service.retrieve(
                KnowledgeRetrieveInput(query="cancel target", top_k=1),
                _access(),
                corpus_version_id=snapshot.corpus_version_id,
            )
        )
        assert await asyncio.to_thread(embedder.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    assert embedder.stopped.is_set()
