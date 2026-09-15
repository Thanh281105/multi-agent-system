"""Fail-closed immutable evidence resolution for Package 8 reporting.

The reporting boundary receives only an ``EvidenceBindingKeyV3``.  It therefore
requires a trusted source of the original ``EvidenceReference`` before it can
reopen a knowledge span or accept catalog/review text.  This module never
derives citation text from a title, a rubric, or a response.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from app.evaluation.benchmark_reporting import (
    EvidenceBindingKeyV3,
    FrozenBenchmarkReportingContractV3,
    ResolvedExactEvidenceV3,
)
from app.knowledge.v2_contracts import (
    ResolvedKnowledgeEvidence,
    sha256_utf8,
    stable_evidence_id,
)
from app.v2.authorization import ResourceAuthorization
from app.v2.contracts import EvidenceKind, EvidenceReference

_CONTENT_ADDRESS_ID = re.compile(r"^(?P<prefix>cor|idx)_[0-9a-f]{60}$")
_HASH_VERSIONED_ID = re.compile(r"^[a-z][a-z0-9_-]{0,32}_[0-9a-f]{60,64}$")
_SHA256 = r"^[a-f0-9]{64}$"


class BenchmarkEvidenceResolutionErrorV3(ValueError):
    """Base error for evidence that cannot be authoritatively resolved."""

    code = "benchmark_evidence_resolution_failed"


class EvidenceMetadataUnavailableErrorV3(BenchmarkEvidenceResolutionErrorV3):
    """The binding cannot be reconstructed as a complete trusted reference."""

    code = "evidence_metadata_unavailable"


class EvidenceProvenanceMismatchErrorV3(BenchmarkEvidenceResolutionErrorV3):
    """A reopened source does not exactly match its immutable binding."""

    code = "evidence_provenance_mismatch"


class UnsupportedEvidenceKindErrorV3(BenchmarkEvidenceResolutionErrorV3):
    """Package 8 has no authority path for this evidence kind."""

    code = "evidence_kind_unsupported"


class UnsafeSynchronousEvidenceResolutionErrorV3(BenchmarkEvidenceResolutionErrorV3):
    """The default synchronous bridge was invoked from a running event loop."""

    code = "unsafe_sync_evidence_resolution"


class ResolvedCitationTextV3(ResolvedExactEvidenceV3):
    """Exact text returned through the existing reporting resolver contract.

    ``ImmutableEvidenceResolverV3`` is structural and accepts this subtype.
    It deliberately adds no serialized fields: reporting revalidates the value as
    ``ResolvedExactEvidenceV3`` before producing blinded artifacts.
    """


class CatalogReviewResolvedEvidenceV3(FrozenBenchmarkReportingContractV3):
    """Typed, immutable catalog/review text supplied by a read-only authority."""

    binding: EvidenceBindingKeyV3
    reference: EvidenceReference
    exact_text: str = Field(min_length=1, max_length=4_000)
    content_sha256: str = Field(pattern=_SHA256)
    authority: Literal["catalog_review_immutable_source"] = (
        "catalog_review_immutable_source"
    )

    @model_validator(mode="after")
    def validate_exact_source_text(self) -> CatalogReviewResolvedEvidenceV3:
        if not self.exact_text.strip():
            raise ValueError("catalog/review evidence text cannot be blank")
        if sha256_utf8(self.exact_text) != self.content_sha256:
            raise ValueError("catalog/review content hash does not match exact text")
        return self


class KnowledgeEvidenceServiceV3(Protocol):
    """The small, authorization-aware KnowledgeService surface used here."""

    def reopen_evidence(
        self,
        reference: EvidenceReference,
        access: ResourceAuthorization,
        *,
        corpus_version_id: str,
        index_manifest_id: str | None = None,
    ) -> Awaitable[ResolvedKnowledgeEvidence]: ...


class KnowledgeCoroutineRunnerV3(Protocol):
    """Explicit bridge for applications that own a safe async execution context."""

    def __call__(
        self,
        factory: Callable[[], Awaitable[ResolvedKnowledgeEvidence]],
    ) -> ResolvedKnowledgeEvidence: ...


class EvidenceReferenceResolverV3(Protocol):
    """Read-only trusted metadata source for an execution-produced binding."""

    def resolve(self, binding: EvidenceBindingKeyV3) -> EvidenceReference | None: ...


class CatalogReviewEvidenceResolverV3(Protocol):
    """Read-only authority for exact catalog or review source text."""

    def resolve(
        self, binding: EvidenceBindingKeyV3
    ) -> CatalogReviewResolvedEvidenceV3 | None: ...


EvidenceReferenceSourceV3 = (
    EvidenceReferenceResolverV3
    | Callable[[EvidenceBindingKeyV3], EvidenceReference | None]
    | Mapping[EvidenceBindingKeyV3, EvidenceReference]
)
CatalogReviewEvidenceSourceV3 = (
    CatalogReviewEvidenceResolverV3
    | Callable[[EvidenceBindingKeyV3], CatalogReviewResolvedEvidenceV3 | None]
    | Mapping[EvidenceBindingKeyV3, CatalogReviewResolvedEvidenceV3]
)


class ImmutableBenchmarkEvidenceResolverV3:
    """Resolve only source-derived evidence for Package 8 citation reporting.

    The constructor deliberately requires both a current ``ResourceAuthorization``
    and the exact published knowledge corpus/index identifiers.  It does not
    synthesize an authorization or look up the current/latest corpus.
    """

    def __init__(
        self,
        knowledge_service: KnowledgeEvidenceServiceV3,
        *,
        authorization: ResourceAuthorization,
        corpus_version_id: str,
        index_manifest_id: str,
        reference_source: EvidenceReferenceSourceV3,
        catalog_review_source: CatalogReviewEvidenceSourceV3 | None = None,
        coroutine_runner: KnowledgeCoroutineRunnerV3 | None = None,
    ) -> None:
        if not isinstance(authorization, ResourceAuthorization):
            raise TypeError("authorization must be an explicit ResourceAuthorization")
        _validate_snapshot_id(corpus_version_id, prefix="cor")
        _validate_snapshot_id(index_manifest_id, prefix="idx")
        if knowledge_service is None:
            raise TypeError("knowledge_service is required")
        if reference_source is None:
            raise TypeError("reference_source is required")

        self._knowledge_service = knowledge_service
        self._authorization = authorization
        self._corpus_version_id = corpus_version_id
        self._index_manifest_id = index_manifest_id
        self._reference_source = _freeze_mapping_source(reference_source)
        self._catalog_review_source = (
            _freeze_mapping_source(catalog_review_source)
            if catalog_review_source is not None
            else None
        )
        self._coroutine_runner = coroutine_runner

    def resolve(self, binding: EvidenceBindingKeyV3) -> ResolvedCitationTextV3:
        """Return exact source text or raise a typed error without a fallback."""

        normalized_binding = _materialize_binding(binding)
        reference = self._resolve_reference(normalized_binding)
        kind = reference.kind
        if kind is EvidenceKind.KNOWLEDGE:
            return self._resolve_knowledge(normalized_binding, reference)
        if kind in {EvidenceKind.CATALOG, EvidenceKind.REVIEW}:
            return self._resolve_catalog_or_review(normalized_binding, reference)
        raise UnsupportedEvidenceKindErrorV3(
            f"Package 8 cannot authoritatively resolve evidence kind {kind.value}"
        )

    def _resolve_reference(self, binding: EvidenceBindingKeyV3) -> EvidenceReference:
        candidate = _read_source(self._reference_source, binding)
        if not isinstance(candidate, EvidenceReference):
            raise EvidenceMetadataUnavailableErrorV3(
                "trusted source did not return a complete EvidenceReference"
            )
        try:
            reference = EvidenceReference.model_validate(
                candidate.model_dump(mode="python")
            )
        except (TypeError, ValidationError) as exc:
            raise EvidenceMetadataUnavailableErrorV3(
                "trusted source returned invalid evidence metadata"
            ) from exc
        if _binding_from_reference(reference) != binding:
            raise EvidenceProvenanceMismatchErrorV3(
                "trusted evidence metadata does not match the requested binding"
            )
        return reference

    def _resolve_knowledge(
        self,
        binding: EvidenceBindingKeyV3,
        reference: EvidenceReference,
    ) -> ResolvedCitationTextV3:
        if (
            reference.chunk_id is None
            or reference.span_id is None
            or reference.url is None
        ):
            raise EvidenceMetadataUnavailableErrorV3(
                "knowledge evidence requires chunk, span, URL, and observed metadata"
            )
        _verify_stable_span_binding(binding)
        reopened = self._run_knowledge_reopen(reference)
        if not isinstance(reopened, ResolvedKnowledgeEvidence):
            raise EvidenceProvenanceMismatchErrorV3(
                "knowledge service did not return resolved immutable evidence"
            )
        try:
            evidence = ResolvedKnowledgeEvidence.model_validate(
                reopened.model_dump(mode="python")
            )
        except (TypeError, ValidationError) as exc:
            raise EvidenceProvenanceMismatchErrorV3(
                "knowledge service returned invalid immutable evidence"
            ) from exc
        if (
            evidence.corpus_version_id != self._corpus_version_id
            or _binding_from_knowledge_evidence(evidence) != binding
            or evidence.title != reference.title
            or str(evidence.url) != str(reference.url)
            or evidence.observed_at != reference.observed_at
        ):
            raise EvidenceProvenanceMismatchErrorV3(
                "reopened knowledge evidence does not match trusted provenance"
            )
        return _resolved_citation(
            binding,
            evidence.excerpt,
        )

    def _run_knowledge_reopen(
        self, reference: EvidenceReference
    ) -> ResolvedKnowledgeEvidence:
        def factory() -> Awaitable[ResolvedKnowledgeEvidence]:
            return self._knowledge_service.reopen_evidence(
                reference,
                self._authorization,
                corpus_version_id=self._corpus_version_id,
                index_manifest_id=self._index_manifest_id,
            )

        if self._coroutine_runner is not None:
            try:
                return self._coroutine_runner(factory)
            except BenchmarkEvidenceResolutionErrorV3:
                raise
            except Exception as exc:
                raise EvidenceProvenanceMismatchErrorV3(
                    "knowledge evidence reopen failed"
                ) from exc
        return _run_coroutine_safely(factory)

    def _resolve_catalog_or_review(
        self,
        binding: EvidenceBindingKeyV3,
        reference: EvidenceReference,
    ) -> ResolvedCitationTextV3:
        if self._catalog_review_source is None:
            raise EvidenceMetadataUnavailableErrorV3(
                "catalog/review evidence has no configured immutable source"
            )
        _validate_immutable_version_id(binding.source_version_id)
        _verify_stable_span_binding(binding, allow_missing_location=True)
        candidate = _read_source(self._catalog_review_source, binding)
        if not isinstance(candidate, CatalogReviewResolvedEvidenceV3):
            raise EvidenceProvenanceMismatchErrorV3(
                "catalog/review source did not return typed exact evidence"
            )
        try:
            resolved = CatalogReviewResolvedEvidenceV3.model_validate(
                candidate.model_dump(mode="python")
            )
        except (TypeError, ValidationError) as exc:
            raise EvidenceProvenanceMismatchErrorV3(
                "catalog/review source returned invalid exact evidence"
            ) from exc
        if (
            resolved.binding != binding
            or resolved.reference != reference
            or _binding_from_reference(resolved.reference) != binding
            or resolved.reference.kind is not reference.kind
        ):
            raise EvidenceProvenanceMismatchErrorV3(
                "catalog/review source does not match trusted provenance"
            )
        return _resolved_citation(
            binding,
            resolved.exact_text,
        )


def _run_coroutine_safely(
    factory: Callable[[], Awaitable[ResolvedKnowledgeEvidence]],
) -> ResolvedKnowledgeEvidence:
    """Use ``asyncio.run`` only from a synchronous context with no active loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise UnsafeSynchronousEvidenceResolutionErrorV3(
            "inject a safe coroutine_runner when resolving evidence from an event loop"
        )

    async def await_factory() -> ResolvedKnowledgeEvidence:
        return await factory()

    try:
        return asyncio.run(await_factory())
    except BenchmarkEvidenceResolutionErrorV3:
        raise
    except Exception as exc:
        raise EvidenceProvenanceMismatchErrorV3(
            "knowledge evidence reopen failed"
        ) from exc


def _materialize_binding(binding: EvidenceBindingKeyV3) -> EvidenceBindingKeyV3:
    if not isinstance(binding, EvidenceBindingKeyV3):
        raise EvidenceProvenanceMismatchErrorV3("evidence binding has an invalid type")
    try:
        return EvidenceBindingKeyV3.model_validate(binding.model_dump(mode="python"))
    except (TypeError, ValidationError) as exc:
        raise EvidenceProvenanceMismatchErrorV3("evidence binding is invalid") from exc


def _binding_from_reference(reference: EvidenceReference) -> EvidenceBindingKeyV3:
    return EvidenceBindingKeyV3(
        evidence_id=reference.evidence_id,
        source_id=reference.source_id,
        source_version_id=reference.source_version_id,
        chunk_id=reference.chunk_id,
        span_id=reference.span_id,
    )


def _binding_from_knowledge_evidence(
    evidence: ResolvedKnowledgeEvidence,
) -> EvidenceBindingKeyV3:
    return EvidenceBindingKeyV3(
        evidence_id=evidence.evidence_id,
        source_id=evidence.source_id,
        source_version_id=evidence.source_version_id,
        chunk_id=evidence.chunk_id,
        span_id=evidence.span_id,
    )


def _resolved_citation(
    binding: EvidenceBindingKeyV3,
    exact_text: str,
) -> ResolvedCitationTextV3:
    try:
        return ResolvedCitationTextV3(
            binding=binding,
            exact_text=exact_text,
        )
    except ValidationError as exc:
        raise EvidenceProvenanceMismatchErrorV3(
            "authoritative source returned unusable exact text"
        ) from exc


def _validate_snapshot_id(value: str, *, prefix: Literal["cor", "idx"]) -> None:
    match = _CONTENT_ADDRESS_ID.fullmatch(value)
    if match is None or match.group("prefix") != prefix:
        raise ValueError(f"{prefix} snapshot identifier must be content-addressed")


def _validate_immutable_version_id(value: str) -> None:
    if _HASH_VERSIONED_ID.fullmatch(value) is None:
        raise EvidenceProvenanceMismatchErrorV3(
            "catalog/review evidence source version is not immutable"
        )


def _verify_stable_span_binding(
    binding: EvidenceBindingKeyV3,
    *,
    allow_missing_location: bool = False,
) -> None:
    if binding.chunk_id is None or binding.span_id is None:
        if (
            allow_missing_location
            and binding.chunk_id is None
            and binding.span_id is None
        ):
            return
        raise EvidenceProvenanceMismatchErrorV3(
            "evidence binding must identify both chunk and span"
        )
    expected = stable_evidence_id(
        source_id=binding.source_id,
        source_version_id=binding.source_version_id,
        chunk_id=binding.chunk_id,
        span_id=binding.span_id,
    )
    if binding.evidence_id != expected:
        raise EvidenceProvenanceMismatchErrorV3(
            "evidence binding does not match its stable span identity"
        )


def _freeze_mapping_source(source: Any) -> Any:
    """Detach mapping-backed test/runtime sources from later mutable lookups."""

    if isinstance(source, Mapping):
        return MappingProxyType(dict(source))
    return source


def _read_source(source: Any, binding: EvidenceBindingKeyV3) -> Any:
    """Read exactly one binding from a mapping, callable, or resolver object."""

    try:
        if isinstance(source, Mapping):
            return source.get(binding)
        resolve = getattr(source, "resolve", None)
        if callable(resolve):
            return resolve(binding)
        if callable(source):
            return source(binding)
    except BenchmarkEvidenceResolutionErrorV3:
        raise
    except Exception as exc:
        raise EvidenceMetadataUnavailableErrorV3(
            "authoritative evidence source lookup failed"
        ) from exc
    raise EvidenceMetadataUnavailableErrorV3(
        "authoritative evidence source does not support exact binding lookup"
    )
