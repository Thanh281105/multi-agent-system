"""Observation-bound reads of the shared, immutable default-tenant corpus.

Benchmark SQL state stays in its canonical isolated tenant. Only knowledge
reads use the corpus owner's tenant, retaining the observation principal,
mode and scopes. This adapter is never installed in the public runtime.
"""

from __future__ import annotations

from app.knowledge.retrieval import KnowledgeRetrievalStore
from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    PublishedKnowledgeSnapshot,
    ResolvedKnowledgeEvidence,
    RetrievalChunk,
)
from app.v2.authorization import AuthorityOverrideError, ResourceAuthorization


class ObservationKnowledgeStoreV3:
    def __init__(
        self,
        store: KnowledgeRetrievalStore,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
    ) -> None:
        self.store = store
        self.snapshot = snapshot
        self.access = access
        self.source_access = access.model_copy(
            update={
                "binding": access.binding.model_copy(update={"tenant_id": "default"})
            }
        )

    def _source_access(
        self, snapshot: PublishedKnowledgeSnapshot, access: ResourceAuthorization
    ) -> ResourceAuthorization:
        if snapshot != self.snapshot or access != self.access:
            raise AuthorityOverrideError
        return self.source_access

    def resolve_published_snapshot(
        self, corpus_version_id: str, *, index_manifest_id: str | None = None
    ) -> PublishedKnowledgeSnapshot:
        if (
            corpus_version_id != self.snapshot.corpus_version_id
            or index_manifest_id not in (None, self.snapshot.index_manifest_id)
        ):
            raise AuthorityOverrideError
        return self.store.resolve_published_snapshot(
            corpus_version_id, index_manifest_id=index_manifest_id
        )

    def list_authorized_sources(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        product_ids: tuple[int, ...] = (),
    ) -> tuple[AuthorizedKnowledgeSource, ...]:
        return self.store.list_authorized_sources(
            snapshot, self._source_access(snapshot, access), product_ids=product_ids
        )

    def load_authorized_chunks(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_ids: frozenset[str],
    ) -> tuple[RetrievalChunk, ...]:
        return self.store.load_authorized_chunks(
            snapshot, self._source_access(snapshot, access), source_ids=source_ids
        )

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
        return self.store.resolve_authorized_evidence(
            snapshot,
            self._source_access(snapshot, access),
            source_id=source_id,
            source_version_id=source_version_id,
            chunk_id=chunk_id,
            span_id=span_id,
        )
