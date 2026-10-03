"""Actual SQL ACL proof for shared-corpus reads from isolated benchmark state."""

import asyncio

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.evaluation.benchmark_cli import (
    BenchmarkCLIError,
    _ReceiptEvidenceAuthority,
    _ReceiptExactEvidenceResolver,
)
from app.evaluation.benchmark_evidence import EvidenceBindingKeyV3
from app.evaluation.v3_knowledge import ObservationKnowledgeStoreV3
from app.knowledge.postgres import PostgresKnowledgeStore
from app.knowledge.retrieval import HybridKnowledgeRetriever
from app.knowledge.service import KnowledgeService
from app.shared.budget import provider_budget_scope
from app.v2.authorization import AuthorityOverrideError
from app.v2.registry import KnowledgeRetrieveInput
from tests.test_knowledge_postgres import (
    _access,
    _acl_manifests,
    _budget,
    _ingestor,
    _manifests,
    _OpenAIShapedHashingEmbedder,
)
from tests.test_knowledge_postgres import postgres_engine as postgres_engine
from tests.test_knowledge_postgres import postgres_sessions as postgres_sessions


@pytest.mark.asyncio
async def test_isolated_observation_retrieves_and_reopens_without_public_acl_change(
    postgres_sessions: sessionmaker[Session],
) -> None:
    store = PostgresKnowledgeStore(postgres_sessions)
    embedder = _OpenAIShapedHashingEmbedder()
    sources, mappings = _manifests(
        corpus_version="evaluation-source-binding-v1",
        content_suffix="Controlled shared-corpus retrieval fixture.",
    )
    with provider_budget_scope(_budget("scope_evaluation_source_binding")):
        build = _ingestor(
            store, embedder, owner="owner_evaluation_source_binding"
        ).ingest(sources, mappings, publish=True)
    snapshot = store.resolve_published_snapshot(build.corpus_version_id)
    access = _access(tenant_id=f"tenant_{'a' * 64}")
    assert store.list_authorized_sources(snapshot, access, product_ids=(80,)) == ()
    bound = ObservationKnowledgeStoreV3(store, snapshot, access)
    authorized = bound.list_authorized_sources(snapshot, access, product_ids=(80,))
    assert {source.source_id for source in authorized} == {"src_sapiens_author"}
    assert bound.source_access.binding.principal_id == access.binding.principal_id
    assert bound.source_access.scopes == access.scopes
    service = KnowledgeService(HybridKnowledgeRetriever(bound, embedder))
    prior_calls = len(embedder.calls)
    with provider_budget_scope(_budget("scope_evaluation_retrieval")):
        result = await service.retrieve(
            KnowledgeRetrieveInput(
                query=(
                    "Sapiens Yuval Noah Harari Homo sapiens agriculture money religion"
                ),
                product_ids=(80,),
            ),
            access,
            corpus_version_id=snapshot.corpus_version_id,
            index_manifest_id=snapshot.index_manifest_id,
        )
    assert len(embedder.calls) > prior_calls
    assert result.result.answerable and result.evidence
    for reference in result.evidence:
        reopened = await service.reopen_evidence(
            reference,
            access,
            corpus_version_id=snapshot.corpus_version_id,
            index_manifest_id=snapshot.index_manifest_id,
        )
        assert reopened.evidence_id == reference.evidence_id
        assert reopened.excerpt == next(
            excerpt.excerpt
            for excerpt in result.result.excerpts
            if excerpt.evidence_id == reference.evidence_id
        )
        binding = EvidenceBindingKeyV3(
            evidence_id=reference.evidence_id,
            source_id=reference.source_id,
            source_version_id=reference.source_version_id,
            chunk_id=reference.chunk_id,
            span_id=reference.span_id,
        )
        resolver_arguments = {
            "knowledge_service": KnowledgeService(
                HybridKnowledgeRetriever(store, embedder)
            ),
            "corpus_version_id": snapshot.corpus_version_id,
            "index_manifest_id": snapshot.index_manifest_id,
            "authorities": {binding: _ReceiptEvidenceAuthority(reference, access)},
        }
        legacy = _ReceiptExactEvidenceResolver(**resolver_arguments)
        with pytest.raises(BenchmarkCLIError, match="could not be reopened"):
            await asyncio.to_thread(legacy.resolve, binding)
        successor = _ReceiptExactEvidenceResolver(
            **resolver_arguments, shared_corpus_reads=True
        )
        exact = await asyncio.to_thread(successor.resolve, binding)
        assert exact.exact_text == reopened.excerpt
    assert store.list_authorized_sources(snapshot, access) == ()
    with pytest.raises(AuthorityOverrideError):
        bound.list_authorized_sources(snapshot, _access(tenant_id=f"tenant_{'b' * 64}"))
    with pytest.raises(AuthorityOverrideError):
        bound.resolve_published_snapshot("cor_foreign")


def test_binding_preserves_scope_mode_and_principal_acl(
    postgres_sessions: sessionmaker[Session],
) -> None:
    store = PostgresKnowledgeStore(postgres_sessions)
    sources, mappings = _acl_manifests()
    with provider_budget_scope(_budget("scope_evaluation_acl_binding")):
        build = _ingestor(
            store, _OpenAIShapedHashingEmbedder(), owner="owner_evaluation_acl_binding"
        ).ingest(sources, mappings, publish=True)
    snapshot = store.resolve_published_snapshot(build.corpus_version_id)
    access = _access(tenant_id=f"tenant_{'c' * 64}", principal_id="other_principal")
    bound = ObservationKnowledgeStoreV3(store, snapshot, access)
    allowed = bound.list_authorized_sources(snapshot, access)
    assert {source.source_id for source in allowed} == {sources.sources[0].source_id}
    assert bound.source_access.binding.mode == access.binding.mode
    with pytest.raises(AuthorityOverrideError):
        bound.list_authorized_sources(
            snapshot,
            access.model_copy(
                update={"scopes": frozenset({"ecommerce.read", "knowledge.premium"})}
            ),
        )
