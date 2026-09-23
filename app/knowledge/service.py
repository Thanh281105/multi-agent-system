# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
# Modified for thanh-v2 from commit
# 94af718da1858b74b3cb4fba05ddd908ac28d9b4.
# Changes: v2 registry conversion, stable evidence reopening, current-scope ACL,
# and cancellation propagation through the shared provider budget context.

"""One bounded retrieval and citation facade for the v2 knowledge capability."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from threading import Event

from app.knowledge.retrieval import (
    HybridKnowledgeRetriever,
    KnowledgeRetrievalBundle,
    KnowledgeRetrievalError,
    KnowledgeRetrievalStore,
)
from app.knowledge.v2_contracts import (
    ResolvedKnowledgeEvidence,
    stable_evidence_id,
)
from app.shared.budget import (
    conservative_text_token_bound,
    current_provider_budget,
    provider_budget_scope,
)
from app.v2.authorization import ResourceAuthorization
from app.v2.contracts import EvidenceKind, EvidenceReference
from app.v2.registry import KnowledgeExcerpt, KnowledgeResult, KnowledgeRetrieveInput


class KnowledgeCitationError(KnowledgeRetrievalError):
    """A citation identity or reopened immutable evidence binding is invalid."""


@dataclass(frozen=True, slots=True)
class KnowledgeServiceResult:
    """Stable capability result plus internal evidence context for Package 4."""

    result: KnowledgeResult
    evidence: tuple[EvidenceReference, ...]
    retrieval: KnowledgeRetrievalBundle

    @property
    def context(self) -> str:
        return self.retrieval.context


class KnowledgeService:
    """Apply request contracts, retrieval, and authorized evidence reopening."""

    def __init__(
        self,
        retriever: HybridKnowledgeRetriever,
        *,
        store: KnowledgeRetrievalStore | None = None,
    ) -> None:
        self.retriever = retriever
        self.store = store or retriever.store

    async def retrieve(
        self,
        request: KnowledgeRetrieveInput,
        access: ResourceAuthorization,
        *,
        corpus_version_id: str,
        index_manifest_id: str | None = None,
    ) -> KnowledgeServiceResult:
        """Return the registered result and separately labelled stable evidence."""

        async def run() -> KnowledgeServiceResult:
            bundle = await self.retriever.retrieve(
                request.query,
                access,
                corpus_version_id=corpus_version_id,
                index_manifest_id=index_manifest_id,
                product_ids=request.product_ids,
                requested_source_ids=request.requested_source_ids,
                limit=request.top_k,
            )
            return _service_result(bundle)

        budget = current_provider_budget()
        if budget is None:
            return await run()

        cancellation = Event()
        inherited_budget = budget
        bound_budget = replace(
            inherited_budget,
            cancellation_requested=lambda: (
                cancellation.is_set() or inherited_budget.cancelled()
            ),
        )
        with provider_budget_scope(bound_budget):
            try:
                return await run()
            except asyncio.CancelledError:
                cancellation.set()
                raise

    async def reopen_evidence(
        self,
        reference: EvidenceReference,
        access: ResourceAuthorization,
        *,
        corpus_version_id: str,
        index_manifest_id: str | None = None,
    ) -> ResolvedKnowledgeEvidence:
        """Reapply current ACL and resolve exactly the referenced immutable span."""

        if (
            reference.kind != EvidenceKind.KNOWLEDGE
            or reference.chunk_id is None
            or reference.span_id is None
        ):
            raise KnowledgeCitationError("knowledge_evidence_reference_invalid")
        expected_evidence_id = stable_evidence_id(
            source_id=reference.source_id,
            source_version_id=reference.source_version_id,
            chunk_id=reference.chunk_id,
            span_id=reference.span_id,
        )
        if reference.evidence_id != expected_evidence_id:
            raise KnowledgeCitationError("knowledge_evidence_identity_mismatch")

        snapshot = await self.retriever.resolve_snapshot(
            corpus_version_id,
            index_manifest_id=index_manifest_id,
        )
        evidence = await asyncio.to_thread(
            self.store.resolve_authorized_evidence,
            snapshot,
            access,
            source_id=reference.source_id,
            source_version_id=reference.source_version_id,
            chunk_id=reference.chunk_id,
            span_id=reference.span_id,
        )
        if (
            evidence.corpus_version_id != snapshot.corpus_version_id
            or evidence.evidence_id != expected_evidence_id
            or evidence.source_id != reference.source_id
            or evidence.source_version_id != reference.source_version_id
            or evidence.chunk_id != reference.chunk_id
            or evidence.span_id != reference.span_id
        ):
            raise KnowledgeCitationError("knowledge_evidence_store_binding_mismatch")
        if (
            reference.title != _display_title(evidence.title)
            or (reference.url is not None and str(reference.url) != str(evidence.url))
            or reference.observed_at != evidence.observed_at
        ):
            raise KnowledgeCitationError("knowledge_evidence_metadata_mismatch")
        return evidence


def _service_result(bundle: KnowledgeRetrievalBundle) -> KnowledgeServiceResult:
    context_bound = conservative_text_token_bound(bundle.context)
    if (
        context_bound != bundle.context_token_count
        or context_bound > bundle.snapshot.retrieval_policy.max_context_tokens
    ):
        raise KnowledgeRetrievalError("knowledge_context_bound_invalid")
    excerpts: list[KnowledgeExcerpt] = []
    references: list[EvidenceReference] = []
    for position, hit in enumerate(bundle.hits, start=1):
        excerpt = hit.chunk.content[hit.span.start_char : hit.span.end_char]
        evidence_id = stable_evidence_id(
            source_id=hit.chunk.source_id,
            source_version_id=hit.chunk.source_version_id,
            chunk_id=hit.chunk.chunk_id,
            span_id=hit.span.span_id,
        )
        excerpts.append(
            KnowledgeExcerpt(
                evidence_id=evidence_id,
                source_id=hit.chunk.source_id,
                source_version_id=hit.chunk.source_version_id,
                chunk_id=hit.chunk.chunk_id,
                span_id=hit.span.span_id,
                excerpt=excerpt,
                score=max(0.0, hit.score),
            )
        )
        references.append(
            EvidenceReference(
                evidence_id=evidence_id,
                source_id=hit.chunk.source_id,
                source_version_id=hit.chunk.source_version_id,
                chunk_id=hit.chunk.chunk_id,
                span_id=hit.span.span_id,
                display_label=f"[C{position}]",
                kind=EvidenceKind.KNOWLEDGE,
                title=_display_title(hit.source.title),
                url=hit.source.url,
                observed_at=hit.source.retrieved_at,
            )
        )
    return KnowledgeServiceResult(
        result=KnowledgeResult(
            corpus_version_id=bundle.snapshot.corpus_version_id,
            excerpts=tuple(excerpts),
            answerable=bundle.answerable,
        ),
        evidence=tuple(references),
        retrieval=bundle,
    )


def _display_title(title: str) -> str:
    """Fit immutable source titles into the narrower public evidence contract."""

    return title.strip()[:300]
