"""Stable citation labels, exact span conversion, and authorized reopening."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from app.knowledge.retrieval import (
    KnowledgeQueryPlan,
    KnowledgeRetrievalBundle,
    KnowledgeRetrievalError,
    RetrievedKnowledgeHit,
    render_retrieval_context,
)
from app.knowledge.service import KnowledgeCitationError, KnowledgeService
from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    IndexBuildSpec,
    KnowledgeSpan,
    PublishedKnowledgeSnapshot,
    ResolvedKnowledgeEvidence,
    RetrievalChunk,
    RetrievalPolicy,
    SourceSupportScope,
    content_addressed_id,
    sha256_text,
    sha256_utf8,
    stable_chunk_id,
    stable_evidence_id,
    stable_span_id,
)
from app.shared.budget import conservative_text_token_bound
from app.v2.authorization import (
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceBinding,
)
from app.v2.contracts import ConversationMode, EvidenceKind
from app.v2.registry import KnowledgeResult, KnowledgeRetrieveInput

NOW = datetime(2026, 9, 9, 8, 30, tzinfo=UTC)


def _access(*, principal_id: str = "citation-user") -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="default",
            principal_id=principal_id,
            mode=ConversationMode.SHOPPER,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read"}),
    )


def _snapshot() -> PublishedKnowledgeSnapshot:
    spec = IndexBuildSpec(
        embedding_model="test-embedding-v1",
        embedding_dimension=32,
        chunker_version="table_chunker_v1",
        enrichment_policy_version="source_enrichment_v1",
    )
    return PublishedKnowledgeSnapshot(
        corpus_version_id=content_addressed_id("cor", {"corpus": "citations"}),
        corpus_name="books-v1",
        corpus_version="2026-09-09",
        index_manifest_id=content_addressed_id("idx", {"index": "citations"}),
        index_fingerprint=spec.index_fingerprint,
        embedding_model=spec.embedding_model,
        embedding_dimension=spec.embedding_dimension,
        chunker_version=spec.chunker_version,
        enrichment_policy_version=spec.enrichment_policy_version,
        query_embedding_fingerprint=spec.query_embedding_fingerprint,
        retrieval_policy=RetrievalPolicy(
            version="hybrid_v1",
            dense_weight=1.0,
            lexical_weight=1.0,
            candidate_limit=30,
            min_dense_relevance=0.8,
            min_lexical_coverage=0.5,
        ),
        published_at=NOW,
    )


@dataclass(frozen=True, slots=True)
class EvidenceFixture:
    source: AuthorizedKnowledgeSource
    chunk: RetrievalChunk
    span: KnowledgeSpan
    resolved: ResolvedKnowledgeEvidence


def _evidence_fixture(
    snapshot: PublishedKnowledgeSnapshot,
    index: int,
    *,
    title: str | None = None,
    content: str | None = None,
) -> EvidenceFixture:
    source = AuthorizedKnowledgeSource(
        source_id=f"src_citation_{index:03d}",
        source_version_id=content_addressed_id("svr", {"citation": index}),
        title=title or f"Citation source {index}",
        url=f"https://example.test/citations/{index}",
        planning_text=f"Evidence about work {index}",
        keywords=(f"work-{index}",),
        support_scope=SourceSupportScope.WORK,
        work_identifier=f"work_citation_{index}",
        retrieved_at=NOW,
    )
    text = content or f"Exact immutable excerpt for work {index}."
    content_hash = sha256_text(text)
    chunk_id = stable_chunk_id(
        source_version_id=source.source_version_id,
        chunker_version=snapshot.chunker_version,
        chunk_index=0,
        content_hash=content_hash,
    )
    span_hash = sha256_utf8(text)
    span = KnowledgeSpan(
        span_id=stable_span_id(
            chunk_id=chunk_id,
            start_char=0,
            end_char=len(text),
            content_hash=span_hash,
        ),
        start_char=0,
        end_char=len(text),
        content_hash=span_hash,
    )
    chunk = RetrievalChunk(
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
        source_id=source.source_id,
        source_version_id=source.source_version_id,
        chunk_id=chunk_id,
        chunk_index=0,
        chunker_version=snapshot.chunker_version,
        title=source.title,
        url=source.url,
        content=text,
        token_count=5,
        content_hash=content_hash,
        vector=tuple(1.0 if position == index % 32 else 0.0 for position in range(32)),
        spans=(span,),
        support_scope=source.support_scope,
        work_identifier=source.work_identifier,
    )
    evidence_id = stable_evidence_id(
        source_id=source.source_id,
        source_version_id=source.source_version_id,
        chunk_id=chunk_id,
        span_id=span.span_id,
    )
    resolved = ResolvedKnowledgeEvidence(
        evidence_id=evidence_id,
        corpus_version_id=snapshot.corpus_version_id,
        source_id=source.source_id,
        source_version_id=source.source_version_id,
        chunk_id=chunk_id,
        span_id=span.span_id,
        title=source.title,
        url=source.url,
        excerpt=text,
        content_hash=span_hash,
        observed_at=source.retrieved_at,
    )
    return EvidenceFixture(source=source, chunk=chunk, span=span, resolved=resolved)


def _bundle(
    snapshot: PublishedKnowledgeSnapshot,
    fixtures: tuple[EvidenceFixture, ...],
) -> KnowledgeRetrievalBundle:
    hits = tuple(
        RetrievedKnowledgeHit(
            source=fixture.source,
            chunk=fixture.chunk,
            span=fixture.span,
            score=0.03 - position * 0.001,
            relevance_score=0.9,
            dense_score=0.9,
            lexical_score=1.0,
            lexical_coverage=1.0,
            dense_rank=position,
            lexical_rank=position,
            channels=("bm25", "dense"),
        )
        for position, fixture in enumerate(fixtures, start=1)
    )
    context = render_retrieval_context(hits)
    return KnowledgeRetrievalBundle(
        snapshot=snapshot,
        query="citation query",
        plan=KnowledgeQueryPlan(
            original_query="citation query",
            queries=("citation query",),
            source_ids=tuple(fixture.source.source_id for fixture in fixtures),
            planner="deterministic",
        ),
        hits=hits,
        trace=(),
        diagnostics=(),
        context_token_count=conservative_text_token_bound(context),
    )


class FakeEvidenceStore:
    def __init__(self, evidence: ResolvedKnowledgeEvidence | BaseException) -> None:
        self.evidence = evidence
        self.calls: list[dict[str, Any]] = []
        self.thread_ids: list[int] = []

    def resolve_authorized_evidence(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_id: str,
        source_version_id: str,
        chunk_id: str,
        span_id: str,
    ) -> ResolvedKnowledgeEvidence:
        self.thread_ids.append(threading.get_ident())
        self.calls.append(
            {
                "snapshot": snapshot,
                "access": access,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "chunk_id": chunk_id,
                "span_id": span_id,
            }
        )
        if isinstance(self.evidence, BaseException):
            raise self.evidence
        return self.evidence


class FakeRetriever:
    def __init__(
        self,
        bundles: tuple[KnowledgeRetrievalBundle, ...],
        store: FakeEvidenceStore,
    ) -> None:
        self.bundles = list(bundles)
        self.store = store
        self.retrieve_calls: list[dict[str, Any]] = []
        self.snapshot_calls: list[tuple[str, str | None]] = []

    async def retrieve(
        self, query: str, access: ResourceAuthorization, **kwargs: Any
    ) -> Any:
        self.retrieve_calls.append({"query": query, "access": access, **kwargs})
        return self.bundles.pop(0)

    async def resolve_snapshot(
        self, corpus_version_id: str, *, index_manifest_id: str | None = None
    ) -> PublishedKnowledgeSnapshot:
        self.snapshot_calls.append((corpus_version_id, index_manifest_id))
        bundle = self.bundles[0] if self.bundles else None
        if bundle is not None:
            return bundle.snapshot
        raise AssertionError("test must leave one snapshot bundle for reopening")


def _service(
    bundles: tuple[KnowledgeRetrievalBundle, ...],
    store: FakeEvidenceStore,
) -> KnowledgeService:
    retriever = FakeRetriever(bundles, store)
    return KnowledgeService(retriever, store=store)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_facade_returns_registered_result_exact_span_and_bounded_title() -> None:
    snapshot = _snapshot()
    fixture = _evidence_fixture(snapshot, 1, title="T" * 500)
    bundle = _bundle(snapshot, (fixture,))
    service = _service((bundle,), FakeEvidenceStore(fixture.resolved))

    response = await service.retrieve(
        KnowledgeRetrieveInput(query="citation query", top_k=1),
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
    )

    assert isinstance(response.result, KnowledgeResult)
    assert response.result.answerable
    assert response.result.corpus_version_id == snapshot.corpus_version_id
    assert response.result.excerpts[0].excerpt == fixture.resolved.excerpt
    assert response.result.excerpts[0].evidence_id == fixture.resolved.evidence_id
    assert response.evidence[0].display_label == "[C1]"
    assert response.evidence[0].evidence_id == fixture.resolved.evidence_id
    assert len(response.evidence[0].title) == 300
    assert response.evidence[0].kind == EvidenceKind.KNOWLEDGE
    assert response.retrieval.hits[0].chunk.support_scope == SourceSupportScope.WORK
    assert response.retrieval.hits[0].chunk.edition_identifier is None
    assert fixture.resolved.excerpt in response.context


@pytest.mark.asyncio
async def test_display_labels_are_response_local_and_never_stable_identity() -> None:
    snapshot = _snapshot()
    first = _evidence_fixture(snapshot, 1)
    second = _evidence_fixture(snapshot, 2)
    service = _service(
        (_bundle(snapshot, (first, second)), _bundle(snapshot, (second, first))),
        FakeEvidenceStore(first.resolved),
    )

    first_response = await service.retrieve(
        KnowledgeRetrieveInput(query="citation query"),
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )
    second_response = await service.retrieve(
        KnowledgeRetrieveInput(query="citation query"),
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )

    first_labels = {
        item.evidence_id: item.display_label for item in first_response.evidence
    }
    second_labels = {
        item.evidence_id: item.display_label for item in second_response.evidence
    }
    assert first_labels[first.resolved.evidence_id] == "[C1]"
    assert second_labels[first.resolved.evidence_id] == "[C2]"
    assert first.resolved.evidence_id == second_response.evidence[1].evidence_id


@pytest.mark.asyncio
async def test_reopen_reapplies_acl_and_returns_exact_excerpt() -> None:
    snapshot = _snapshot()
    fixture = _evidence_fixture(snapshot, 1)
    bundle = _bundle(snapshot, (fixture,))
    store = FakeEvidenceStore(fixture.resolved)
    retriever = FakeRetriever((bundle, bundle), store)
    service = KnowledgeService(retriever, store=store)  # type: ignore[arg-type]
    access = _access()
    response = await service.retrieve(
        KnowledgeRetrieveInput(query="citation query"),
        access,
        corpus_version_id=snapshot.corpus_version_id,
    )
    reference = response.evidence[0].model_copy(update={"display_label": "[C7]"})
    loop_thread = threading.get_ident()

    reopened = await service.reopen_evidence(
        reference,
        access,
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
    )

    assert reopened == fixture.resolved
    assert (
        reopened.excerpt
        == fixture.chunk.content[fixture.span.start_char : fixture.span.end_char]
    )
    assert reopened.content_hash == sha256_utf8(reopened.excerpt)
    assert store.calls[0]["access"] is access
    assert store.calls[0]["source_id"] == reference.source_id
    assert store.calls[0]["source_version_id"] == reference.source_version_id
    assert store.calls[0]["chunk_id"] == reference.chunk_id
    assert store.calls[0]["span_id"] == reference.span_id
    assert store.thread_ids[0] != loop_thread


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_id", "src_wrong_source"),
        ("source_version_id", content_addressed_id("svr", {"wrong": 1})),
        ("chunk_id", content_addressed_id("chk", {"wrong": 1})),
        ("span_id", content_addressed_id("spn", {"wrong": 1})),
    ),
)
async def test_reopen_rejects_any_identity_component_changed_under_same_id(
    field: str, value: str
) -> None:
    snapshot = _snapshot()
    fixture = _evidence_fixture(snapshot, 1)
    bundle = _bundle(snapshot, (fixture,))
    store = FakeEvidenceStore(fixture.resolved)
    retriever = FakeRetriever((bundle, bundle), store)
    service = KnowledgeService(retriever, store=store)  # type: ignore[arg-type]
    response = await service.retrieve(
        KnowledgeRetrieveInput(query="citation query"),
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )
    tampered = response.evidence[0].model_copy(update={field: value})

    with pytest.raises(KnowledgeCitationError, match="identity_mismatch"):
        await service.reopen_evidence(
            tampered,
            _access(),
            corpus_version_id=snapshot.corpus_version_id,
        )

    assert store.calls == []


@pytest.mark.asyncio
async def test_reopen_rejects_store_result_for_another_valid_evidence() -> None:
    snapshot = _snapshot()
    requested = _evidence_fixture(snapshot, 1)
    wrong = _evidence_fixture(snapshot, 2)
    bundle = _bundle(snapshot, (requested,))
    store = FakeEvidenceStore(wrong.resolved)
    retriever = FakeRetriever((bundle, bundle), store)
    service = KnowledgeService(retriever, store=store)  # type: ignore[arg-type]
    response = await service.retrieve(
        KnowledgeRetrieveInput(query="citation query"),
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )

    with pytest.raises(KnowledgeCitationError, match="store_binding_mismatch"):
        await service.reopen_evidence(
            response.evidence[0],
            _access(),
            corpus_version_id=snapshot.corpus_version_id,
        )


@pytest.mark.asyncio
async def test_reopen_propagates_current_acl_denial() -> None:
    snapshot = _snapshot()
    fixture = _evidence_fixture(snapshot, 1)
    bundle = _bundle(snapshot, (fixture,))
    store = FakeEvidenceStore(AuthorizationDeniedError())
    retriever = FakeRetriever((bundle, bundle), store)
    service = KnowledgeService(retriever, store=store)  # type: ignore[arg-type]
    response = await service.retrieve(
        KnowledgeRetrieveInput(query="citation query"),
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )
    changed_access = _access(principal_id="current-user-with-different-binding")

    with pytest.raises(AuthorizationDeniedError):
        await service.reopen_evidence(
            response.evidence[0],
            changed_access,
            corpus_version_id=snapshot.corpus_version_id,
        )

    assert store.calls[0]["access"] is changed_access


@pytest.mark.asyncio
async def test_empty_retrieval_converts_to_explicit_no_answer() -> None:
    snapshot = _snapshot()
    empty = _bundle(snapshot, ())
    service = _service((empty,), FakeEvidenceStore(LookupError()))

    response = await service.retrieve(
        KnowledgeRetrieveInput(query="unsupported request"),
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
    )

    assert response.result == KnowledgeResult(
        corpus_version_id=snapshot.corpus_version_id,
        excerpts=(),
        answerable=False,
    )
    assert response.evidence == ()
    assert response.context == ""


@pytest.mark.asyncio
async def test_facade_rejects_tampered_context_accounting() -> None:
    snapshot = _snapshot()
    fixture = _evidence_fixture(snapshot, 1)
    bundle = replace(_bundle(snapshot, (fixture,)), context_token_count=1)
    service = _service((bundle,), FakeEvidenceStore(fixture.resolved))

    with pytest.raises(KnowledgeRetrievalError, match="context_bound_invalid"):
        await service.retrieve(
            KnowledgeRetrieveInput(query="citation query"),
            _access(),
            corpus_version_id=snapshot.corpus_version_id,
        )
