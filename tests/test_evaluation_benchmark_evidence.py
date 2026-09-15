"""Focused contract tests for Package 8 immutable evidence resolution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.evaluation.benchmark_evidence import (
    CatalogReviewResolvedEvidenceV3,
    EvidenceMetadataUnavailableErrorV3,
    EvidenceProvenanceMismatchErrorV3,
    ImmutableBenchmarkEvidenceResolverV3,
    UnsafeSynchronousEvidenceResolutionErrorV3,
)
from app.evaluation.benchmark_reporting import (
    EvidenceBindingKeyV3,
    ResolvedExactEvidenceV3,
)
from app.knowledge.v2_contracts import (
    ResolvedKnowledgeEvidence,
    sha256_utf8,
    stable_evidence_id,
)
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import ConversationMode, EvidenceKind, EvidenceReference

CORPUS_VERSION_ID = "cor_" + "a" * 60
INDEX_MANIFEST_ID = "idx_" + "b" * 60
SOURCE_VERSION_ID = "svr_" + "c" * 60
CHUNK_ID = "chk_" + "d" * 60
SPAN_ID = "spn_" + "e" * 60
OBSERVED_AT = datetime(2026, 9, 15, 7, 0, tzinfo=UTC)


class _FakeKnowledgeService:
    def __init__(self, evidence: ResolvedKnowledgeEvidence | None) -> None:
        self.evidence = evidence
        self.calls: list[
            tuple[EvidenceReference, ResourceAuthorization, str, str | None]
        ] = []

    async def reopen_evidence(
        self,
        reference: EvidenceReference,
        access: ResourceAuthorization,
        *,
        corpus_version_id: str,
        index_manifest_id: str | None = None,
    ) -> ResolvedKnowledgeEvidence | None:
        self.calls.append((reference, access, corpus_version_id, index_manifest_id))
        return self.evidence


class _ReferenceSource:
    def __init__(self, reference: EvidenceReference | None) -> None:
        self.reference = reference
        self.calls: list[EvidenceBindingKeyV3] = []

    def resolve(self, binding: EvidenceBindingKeyV3) -> EvidenceReference | None:
        self.calls.append(binding)
        return self.reference


class _CatalogReviewSource:
    def __init__(self, evidence: CatalogReviewResolvedEvidenceV3 | None) -> None:
        self.evidence = evidence
        self.calls: list[EvidenceBindingKeyV3] = []

    def resolve(
        self, binding: EvidenceBindingKeyV3
    ) -> CatalogReviewResolvedEvidenceV3 | None:
        self.calls.append(binding)
        return self.evidence


def test_knowledge_resolution_passes_pinned_isolation_inputs() -> None:
    binding = _knowledge_binding()
    reference = _knowledge_reference(binding)
    expected_excerpt = "Exact source excerpt; it is never composed by the resolver."
    reopened = _knowledge_evidence(binding, excerpt=expected_excerpt)
    service = _FakeKnowledgeService(reopened)
    authorization = _authorization()
    resolver = _resolver(
        service,
        authorization=authorization,
        reference_source=_ReferenceSource(reference),
        coroutine_runner=_asyncio_runner,
    )

    resolved = resolver.resolve(binding)

    assert resolved.exact_text == expected_excerpt
    assert resolved.binding == binding
    assert ResolvedExactEvidenceV3.model_validate(resolved.model_dump()) == (
        ResolvedExactEvidenceV3(binding=binding, exact_text=expected_excerpt)
    )
    assert len(service.calls) == 1
    called_reference, called_access, corpus_id, index_id = service.calls[0]
    assert called_reference == reference
    assert called_access is authorization
    assert corpus_id == CORPUS_VERSION_ID
    assert index_id == INDEX_MANIFEST_ID


def test_knowledge_resolution_rejects_metadata_binding_mismatch() -> None:
    binding = _knowledge_binding()
    bad_reference = _knowledge_reference(binding).model_copy(
        update={"source_id": "src_other"}
    )
    service = _FakeKnowledgeService(
        _knowledge_evidence(binding, excerpt="Exact source.")
    )
    resolver = _resolver(
        service,
        reference_source=_ReferenceSource(bad_reference),
        coroutine_runner=_asyncio_runner,
    )

    with pytest.raises(EvidenceProvenanceMismatchErrorV3, match="metadata"):
        resolver.resolve(binding)

    assert service.calls == []


def test_knowledge_resolution_rejects_reopened_mismatch() -> None:
    binding = _knowledge_binding()
    reference = _knowledge_reference(binding)
    mismatched = _knowledge_evidence(binding, excerpt="Exact source.").model_copy(
        update={"title": "Different source title"}
    )
    service = _FakeKnowledgeService(mismatched)
    resolver = _resolver(
        service,
        reference_source=_ReferenceSource(reference),
        coroutine_runner=_asyncio_runner,
    )

    with pytest.raises(EvidenceProvenanceMismatchErrorV3, match="provenance"):
        resolver.resolve(binding)

    assert service.calls


def test_nonknowledge_without_authority_is_rejected() -> None:
    binding = _catalog_binding()
    reference = _catalog_reference(binding, kind=EvidenceKind.CATALOG)
    resolver = _resolver(
        _FakeKnowledgeService(None),
        reference_source=_ReferenceSource(reference),
    )

    with pytest.raises(EvidenceMetadataUnavailableErrorV3, match="no configured"):
        resolver.resolve(binding)


@pytest.mark.parametrize("kind", (EvidenceKind.CATALOG, EvidenceKind.REVIEW))
def test_catalog_and_review_resolution_requires_exact_immutable_payload(
    kind: EvidenceKind,
) -> None:
    binding = _catalog_binding()
    reference = _catalog_reference(binding, kind=kind)
    exact_text = "snapshot_price_vnd: 120000\nsampled_review_count: 4 review"
    catalog_source = _CatalogReviewSource(
        CatalogReviewResolvedEvidenceV3(
            binding=binding,
            reference=reference,
            exact_text=exact_text,
            content_sha256=sha256_utf8(exact_text),
        )
    )
    resolver = _resolver(
        _FakeKnowledgeService(None),
        reference_source=_ReferenceSource(reference),
        catalog_review_source=catalog_source,
    )

    resolved = resolver.resolve(binding)

    assert resolved.exact_text == exact_text
    assert catalog_source.calls == [binding]


def test_knowledge_resolution_fails_closed_inside_an_active_event_loop() -> None:
    binding = _knowledge_binding()
    reference = _knowledge_reference(binding)
    service = _FakeKnowledgeService(
        _knowledge_evidence(binding, excerpt="Exact source.")
    )
    resolver = _resolver(service, reference_source=_ReferenceSource(reference))

    async def resolve_from_loop() -> None:
        with pytest.raises(UnsafeSynchronousEvidenceResolutionErrorV3):
            resolver.resolve(binding)

    asyncio.run(resolve_from_loop())
    assert service.calls == []


def test_knowledge_resolution_never_falls_back_to_titles_or_other_text() -> None:
    binding = _knowledge_binding()
    reference = _knowledge_reference(binding)
    service = _FakeKnowledgeService(None)
    resolver = _resolver(
        service,
        reference_source=_ReferenceSource(reference),
        coroutine_runner=_asyncio_runner,
    )

    with pytest.raises(EvidenceProvenanceMismatchErrorV3, match="did not return"):
        resolver.resolve(binding)

    assert len(service.calls) == 1


def _resolver(
    service: _FakeKnowledgeService,
    *,
    authorization: ResourceAuthorization | None = None,
    reference_source: _ReferenceSource,
    catalog_review_source: _CatalogReviewSource | None = None,
    coroutine_runner=None,
) -> ImmutableBenchmarkEvidenceResolverV3:
    return ImmutableBenchmarkEvidenceResolverV3(
        service,
        authorization=authorization or _authorization(),
        corpus_version_id=CORPUS_VERSION_ID,
        index_manifest_id=INDEX_MANIFEST_ID,
        reference_source=reference_source,
        catalog_review_source=catalog_review_source,
        coroutine_runner=coroutine_runner,
    )


def _asyncio_runner(factory):
    return asyncio.run(factory())


def _authorization() -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="tenant_primary",
            principal_id="principal_primary",
            mode=ConversationMode.SHOPPER,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read"}),
    )


def _knowledge_binding() -> EvidenceBindingKeyV3:
    return EvidenceBindingKeyV3(
        evidence_id=stable_evidence_id(
            source_id="src_primary",
            source_version_id=SOURCE_VERSION_ID,
            chunk_id=CHUNK_ID,
            span_id=SPAN_ID,
        ),
        source_id="src_primary",
        source_version_id=SOURCE_VERSION_ID,
        chunk_id=CHUNK_ID,
        span_id=SPAN_ID,
    )


def _knowledge_reference(binding: EvidenceBindingKeyV3) -> EvidenceReference:
    return EvidenceReference(
        **binding.model_dump(),
        display_label="[C1]",
        kind=EvidenceKind.KNOWLEDGE,
        title="Immutable knowledge source",
        url="https://example.com/knowledge-source",
        observed_at=OBSERVED_AT,
    )


def _knowledge_evidence(
    binding: EvidenceBindingKeyV3, *, excerpt: str
) -> ResolvedKnowledgeEvidence:
    return ResolvedKnowledgeEvidence(
        **binding.model_dump(),
        corpus_version_id=CORPUS_VERSION_ID,
        title="Immutable knowledge source",
        url="https://example.com/knowledge-source",
        excerpt=excerpt,
        content_hash=sha256_utf8(excerpt),
        observed_at=OBSERVED_AT,
    )


def _catalog_binding() -> EvidenceBindingKeyV3:
    return EvidenceBindingKeyV3(
        evidence_id="evidence_catalog",
        source_id="catalog_primary",
        source_version_id="cat_" + "f" * 64,
    )


def _catalog_reference(
    binding: EvidenceBindingKeyV3, *, kind: EvidenceKind
) -> EvidenceReference:
    return EvidenceReference(
        **binding.model_dump(),
        display_label="[C1]",
        kind=kind,
        title="Immutable catalog/review snapshot",
        observed_at=OBSERVED_AT,
    )
