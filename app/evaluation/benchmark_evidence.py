"""Fail-closed immutable evidence resolution for Package 8 reporting.

The reporting boundary receives only an ``EvidenceBindingKeyV3``.  It therefore
requires a trusted source of the original ``EvidenceReference`` before it can
reopen a knowledge span or accept catalog/review text.  This module never
derives citation text from a title, a rubric, or a response.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from app.evaluation.protocol import canonical_json_bytes
from app.knowledge.v2_contracts import (
    ResolvedKnowledgeEvidence,
    sha256_utf8,
    stable_evidence_id,
)
from app.v2.authorization import DEMO_STORE_ID, ResourceAuthorization
from app.v2.contracts import ConversationMode, EvidenceKind, EvidenceReference

_CONTENT_ADDRESS_ID = re.compile(r"^(?P<prefix>cor|idx)_[0-9a-f]{60}$")
_HASH_VERSIONED_ID = re.compile(r"^[a-z][a-z0-9_-]{0,32}_[0-9a-f]{60,64}$")
_SHA256 = r"^[a-f0-9]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.-]{2,127}$"
_NAMESPACE_ID = r"^namespace_[a-f0-9]{64}$"


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


class SandboxEvidenceAuthorityUnavailableErrorV3(EvidenceMetadataUnavailableErrorV3):
    """No immutable, receipt-bound sandbox source was supplied."""

    code = "sandbox_evidence_authority_unavailable"


class SandboxEvidenceAuthorizationMismatchErrorV3(EvidenceProvenanceMismatchErrorV3):
    """Sandbox evidence belongs to a different current authorization."""

    code = "sandbox_evidence_authorization_mismatch"


class SandboxEvidenceProvenanceMismatchErrorV3(EvidenceProvenanceMismatchErrorV3):
    """Sandbox fixture, subject, offer, or exact record does not match."""

    code = "sandbox_evidence_provenance_mismatch"


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


class SandboxResolvedEvidenceV3(FrozenBenchmarkReportingContractV3):
    """Exact sandbox read record bound to a reset fixture and receipt authority.

    The canonical fixture is carried as immutable JSON text so mapping-backed
    resolver inputs cannot be changed after construction.  Resolution verifies
    its hash and reconstructs the server-owned inventory record from that
    fixture; neither answer text nor presentation metadata is an authority.
    """

    binding: EvidenceBindingKeyV3
    reference: EvidenceReference
    authorization: ResourceAuthorization
    fixture_id: str = Field(pattern=_IDENTIFIER)
    fixture_sha256: str = Field(pattern=_SHA256)
    reset_revision: int = Field(ge=1)
    fixture_payload_json: str = Field(min_length=1, max_length=100_000)
    execution_namespace_id: str = Field(pattern=_NAMESPACE_ID)
    record_kind: Literal[
        "merchant_inventory_offer",
        "shopper_cart_summary",
        "shopper_cart_item",
        "shopper_checkout_summary",
        "shopper_checkout_item",
    ] = "merchant_inventory_offer"
    snapshot_version_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    fixture_offer_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    offer_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    offer_version: int | None = Field(default=None, ge=1)
    fixture_cart_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    cart_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    product_id: int | None = Field(default=None, ge=1)
    subject_id: str = Field(pattern=_IDENTIFIER)
    exact_text: str = Field(min_length=1, max_length=4_000)
    content_sha256: str = Field(pattern=_SHA256)
    authority: Literal["sandbox_reset_receipt"] = "sandbox_reset_receipt"

    @model_validator(mode="after")
    def validate_exact_source_text(self) -> SandboxResolvedEvidenceV3:
        if not self.exact_text.strip():
            raise ValueError("sandbox evidence text cannot be blank")
        if sha256_utf8(self.exact_text) != self.content_sha256:
            raise ValueError("sandbox content hash does not match exact text")
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


class SandboxEvidenceResolverV3(Protocol):
    """Read-only authority for one receipt-bound sandbox inventory record."""

    def resolve(
        self, binding: EvidenceBindingKeyV3
    ) -> SandboxResolvedEvidenceV3 | None: ...


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
SandboxEvidenceSourceV3 = (
    SandboxEvidenceResolverV3
    | Callable[[EvidenceBindingKeyV3], SandboxResolvedEvidenceV3 | None]
    | Mapping[EvidenceBindingKeyV3, SandboxResolvedEvidenceV3]
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
        sandbox_source: SandboxEvidenceSourceV3 | None = None,
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
        self._sandbox_source = (
            _freeze_mapping_source(sandbox_source)
            if sandbox_source is not None
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
        if kind is EvidenceKind.SANDBOX:
            return self._resolve_sandbox(normalized_binding, reference)
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

    def _resolve_sandbox(
        self,
        binding: EvidenceBindingKeyV3,
        reference: EvidenceReference,
    ) -> ResolvedCitationTextV3:
        if self._sandbox_source is None:
            raise SandboxEvidenceAuthorityUnavailableErrorV3(
                "sandbox evidence has no configured immutable authority"
            )
        candidate = _read_source(self._sandbox_source, binding)
        if not isinstance(candidate, SandboxResolvedEvidenceV3):
            raise SandboxEvidenceAuthorityUnavailableErrorV3(
                "sandbox source did not return typed exact evidence"
            )
        try:
            resolved = SandboxResolvedEvidenceV3.model_validate(
                candidate.model_dump(mode="python")
            )
        except (TypeError, ValidationError) as exc:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox source returned invalid exact evidence"
            ) from exc

        if resolved.authorization != self._authorization:
            raise SandboxEvidenceAuthorizationMismatchErrorV3(
                "sandbox evidence authorization differs from the current authority"
            )
        access = self._authorization
        if access.binding.store_id != DEMO_STORE_ID:
            raise SandboxEvidenceAuthorizationMismatchErrorV3(
                "sandbox evidence is outside the authorized demo store"
            )
        if (
            resolved.binding != binding
            or resolved.reference != reference
            or resolved.reference.kind is not EvidenceKind.SANDBOX
            or _binding_from_reference(resolved.reference) != binding
        ):
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox source does not match trusted reference provenance"
            )

        try:
            _validate_immutable_version_id(binding.source_version_id)
            _verify_stable_span_binding(binding)
        except EvidenceProvenanceMismatchErrorV3 as exc:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox binding is not an immutable exact span"
            ) from exc
        if resolved.record_kind == "merchant_inventory_offer":
            return self._resolve_sandbox_inventory(binding, reference, resolved)
        return self._resolve_sandbox_cart_or_checkout(binding, reference, resolved)

    def _resolve_sandbox_inventory(
        self,
        binding: EvidenceBindingKeyV3,
        reference: EvidenceReference,
        resolved: SandboxResolvedEvidenceV3,
    ) -> ResolvedCitationTextV3:
        if (
            resolved.snapshot_version_id is None
            or resolved.fixture_offer_id is None
            or resolved.offer_id is None
            or resolved.offer_version is None
            or resolved.fixture_cart_id is not None
            or resolved.cart_id is not None
            or resolved.product_id is not None
        ):
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox inventory authority fields are incomplete"
            )
        access = self._authorization
        if (
            access.binding.mode is not ConversationMode.MERCHANT
            or "merchant.read" not in access.scopes
        ):
            raise SandboxEvidenceAuthorizationMismatchErrorV3(
                "current authority cannot read merchant inventory"
            )
        fixture_offer = _validated_sandbox_fixture_offer(resolved)
        expected_offer_id = _sandbox_resource_id(
            "offer",
            resolved.execution_namespace_id,
            resolved.fixture_offer_id,
        )
        if resolved.offer_id != expected_offer_id:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox offer does not match its execution namespace"
            )
        expected_subject_id = f"offer_{expected_offer_id}"
        if resolved.subject_id != expected_subject_id:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox subject does not match its fixture offer"
            )
        expected_source_id = f"sandbox_inventory_{expected_offer_id}"
        if binding.source_id != expected_source_id:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox source does not match its fixture offer"
            )
        expected_version = fixture_offer["version"]
        if resolved.offer_version != expected_version:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox offer version does not match the reset fixture"
            )
        expected_text = _sandbox_inventory_text(fixture_offer)
        if resolved.exact_text != expected_text:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox exact text does not match the reset fixture"
            )
        _verify_sandbox_reference(
            binding=binding,
            reference=reference,
            source_id=expected_source_id,
            title=f"Demo inventory — offer {expected_offer_id}",
            exact_text=expected_text,
        )
        return _resolved_citation(binding, expected_text)

    def _resolve_sandbox_cart_or_checkout(
        self,
        binding: EvidenceBindingKeyV3,
        reference: EvidenceReference,
        resolved: SandboxResolvedEvidenceV3,
    ) -> ResolvedCitationTextV3:
        if (
            resolved.fixture_cart_id is None
            or resolved.cart_id is None
            or resolved.snapshot_version_id is not None
            or resolved.fixture_offer_id is not None
            or resolved.offer_id is not None
            or resolved.offer_version is not None
        ):
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox cart authority fields are incomplete"
            )
        access = self._authorization
        if (
            access.binding.mode is not ConversationMode.SHOPPER
            or "ecommerce.read" not in access.scopes
        ):
            raise SandboxEvidenceAuthorizationMismatchErrorV3(
                "current authority cannot read the shopper cart"
            )
        cart, lines = _validated_sandbox_fixture_cart(resolved)
        expected_cart_id = _sandbox_resource_id(
            "cart",
            resolved.execution_namespace_id,
            resolved.fixture_cart_id,
        )
        if resolved.cart_id != expected_cart_id:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox cart does not match its execution namespace"
            )
        is_checkout = resolved.record_kind.startswith("shopper_checkout_")
        is_item = resolved.record_kind.endswith("_item")
        if is_checkout:
            _validate_checkout_preview_authority(resolved)
        prefix = "sandbox_checkout" if is_checkout else "sandbox_cart"
        title_prefix = "Demo checkout" if is_checkout else "Demo cart"
        if is_item:
            line = _sandbox_cart_line(lines, resolved.product_id)
            product_id = line["product_id"]
            expected_subject_id = f"product_{product_id}"
            expected_source_id = f"{prefix}_{expected_cart_id}_{product_id}"
            expected_title = (
                f"{title_prefix} item — product {product_id} in {expected_cart_id}"
            )
            expected_text = _sandbox_cart_item_text(line)
        else:
            if resolved.product_id is not None:
                raise SandboxEvidenceProvenanceMismatchErrorV3(
                    "sandbox cart summary cannot select a product"
                )
            expected_subject_id = expected_cart_id
            expected_source_id = f"{prefix}_{expected_cart_id}"
            if is_checkout:
                expected_title = f"Demo checkout preview — {expected_cart_id}"
                expected_text = _sandbox_checkout_summary_text(cart, lines)
            else:
                expected_title = f"Demo cart — {expected_cart_id}"
                expected_text = _sandbox_cart_summary_text(
                    expected_cart_id,
                    cart,
                    lines,
                )
        if resolved.subject_id != expected_subject_id:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox subject does not match its fixture cart record"
            )
        if resolved.exact_text != expected_text:
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox exact text does not match the reset fixture"
            )
        _verify_sandbox_reference(
            binding=binding,
            reference=reference,
            source_id=expected_source_id,
            title=expected_title,
            exact_text=expected_text,
        )
        return _resolved_citation(binding, expected_text)


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


def _validated_sandbox_fixture_offer(
    resolved: SandboxResolvedEvidenceV3,
) -> dict[str, Any]:
    payload = _validated_sandbox_fixture_payload(resolved)
    if payload.get("cart") is not None:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture state domain does not match inventory evidence"
        )
    merchant = payload.get("merchant")
    if not isinstance(merchant, dict):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture has no merchant inventory authority"
        )
    if merchant.get("snapshot_version_id") != resolved.snapshot_version_id:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox inventory snapshot does not match the reset fixture"
        )
    offers = merchant.get("offers")
    if not isinstance(offers, list):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture offers are invalid"
        )
    matches = [
        item
        for item in offers
        if isinstance(item, dict) and item.get("offer_id") == resolved.fixture_offer_id
    ]
    if len(matches) != 1:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture does not contain exactly one bound offer"
        )
    offer = matches[0]
    if (
        type(offer.get("price_vnd")) is not int
        or type(offer.get("available_quantity")) is not int
        or type(offer.get("version")) is not int
        or offer["price_vnd"] < 0
        or offer["available_quantity"] < 0
        or offer["version"] < 1
    ):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture offer values are invalid"
        )
    return offer


def _validated_sandbox_fixture_payload(
    resolved: SandboxResolvedEvidenceV3,
) -> dict[str, Any]:
    try:
        payload = json.loads(resolved.fixture_payload_json)
    except (TypeError, ValueError) as exc:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture payload is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture payload is not an object"
        )
    canonical_payload = canonical_json_bytes(payload).decode("utf-8")
    if canonical_payload != resolved.fixture_payload_json:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture payload is not canonical"
        )
    if hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest() != (
        resolved.fixture_sha256
    ):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture hash does not match its canonical payload"
        )
    if (
        payload.get("fixture_id") != resolved.fixture_id
        or payload.get("reset_revision") != resolved.reset_revision
    ):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture identity does not match"
        )
    return payload


def _validated_sandbox_fixture_cart(
    resolved: SandboxResolvedEvidenceV3,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    payload = _validated_sandbox_fixture_payload(resolved)
    if payload.get("merchant") is not None:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture state domain does not match cart evidence"
        )
    cart = payload.get("cart")
    if not isinstance(cart, dict):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture has no shopper cart authority"
        )
    if cart.get("cart_id") != resolved.fixture_cart_id:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture cart identity does not match"
        )
    version = cart.get("version")
    lines = cart.get("lines")
    if (
        type(version) is not int
        or version < 1
        or not isinstance(lines, list)
        or not lines
    ):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture cart values are invalid"
        )
    materialized: list[dict[str, Any]] = []
    for item in lines:
        if not isinstance(item, dict):
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox fixture cart line is invalid"
            )
        product_id = item.get("product_id")
        quantity = item.get("quantity")
        unit_price = item.get("unit_price_vnd")
        if (
            type(product_id) is not int
            or type(quantity) is not int
            or type(unit_price) is not int
            or product_id < 1
            or quantity < 1
            or unit_price <= 0
        ):
            raise SandboxEvidenceProvenanceMismatchErrorV3(
                "sandbox fixture cart line values are invalid"
            )
        materialized.append(item)
    product_ids = [item["product_id"] for item in materialized]
    if len(product_ids) != len(set(product_ids)):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture cart product identities are ambiguous"
        )
    return cart, tuple(materialized)


def _validate_checkout_preview_authority(
    resolved: SandboxResolvedEvidenceV3,
) -> None:
    payload = _validated_sandbox_fixture_payload(resolved)
    cart = payload.get("cart")
    proposal = payload.get("proposal_parameters")
    if (
        payload.get("target_capability_id") != "shopper.checkout.propose"
        or payload.get("confirmed_proposal_id") is not None
        or not isinstance(cart, dict)
        or not isinstance(proposal, dict)
        or proposal.get("kind") != "checkout_proposal"
        or proposal.get("capability_id") != "shopper.checkout.propose"
        or proposal.get("cart_id") != resolved.fixture_cart_id
        or proposal.get("expected_version") != cart.get("version")
    ):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture cannot authorize an exact checkout preview"
        )


def _sandbox_cart_line(
    lines: tuple[dict[str, Any], ...],
    product_id: int | None,
) -> dict[str, Any]:
    matches = [item for item in lines if item["product_id"] == product_id]
    if product_id is None or len(matches) != 1:
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox fixture does not contain exactly one bound cart item"
        )
    return matches[0]


def _sandbox_cart_total(lines: tuple[dict[str, Any], ...]) -> int:
    return sum(item["quantity"] * item["unit_price_vnd"] for item in lines)


def _sandbox_cart_summary_text(
    cart_id: str,
    cart: Mapping[str, Any],
    lines: tuple[dict[str, Any], ...],
) -> str:
    return (
        f"demo_cart_id: {cart_id}\n"
        f"demo_cart_version: {cart['version']}\n"
        f"demo_cart_total_vnd: {_sandbox_cart_total(lines)} VND"
    )


def _sandbox_checkout_summary_text(
    cart: Mapping[str, Any],
    lines: tuple[dict[str, Any], ...],
) -> str:
    return (
        "demo_checkout_can_checkout: True\n"
        "demo_checkout_issues: none\n"
        f"demo_cart_version: {cart['version']}\n"
        f"demo_cart_total_vnd: {_sandbox_cart_total(lines)} VND"
    )


def _sandbox_cart_item_text(line: Mapping[str, Any]) -> str:
    total = line["quantity"] * line["unit_price_vnd"]
    return (
        f"demo_price_vnd: {line['unit_price_vnd']} VND\n"
        f"demo_quantity: {line['quantity']} item\n"
        f"demo_line_total_vnd: {total} VND"
    )


def _verify_sandbox_reference(
    *,
    binding: EvidenceBindingKeyV3,
    reference: EvidenceReference,
    source_id: str,
    title: str,
    exact_text: str,
) -> None:
    expected_chunk_id = _sandbox_tool_id(
        "tch",
        {
            "source": source_id,
            "version": binding.source_version_id,
            "text": exact_text,
        },
    )
    expected_span_id = _sandbox_tool_id(
        "tsp",
        {"chunk": expected_chunk_id, "start": 0, "end": len(exact_text)},
    )
    if (
        binding.source_id != source_id
        or binding.chunk_id != expected_chunk_id
        or binding.span_id != expected_span_id
        or reference.title != title
        or reference.url is not None
    ):
        raise SandboxEvidenceProvenanceMismatchErrorV3(
            "sandbox reference does not identify the canonical fixture record"
        )


def _sandbox_resource_id(kind: str, namespace_id: str, source_id: str) -> str:
    material = f"{namespace_id}:{source_id}".encode("utf-8")
    return f"{kind}_{hashlib.sha256(material).hexdigest()[:48]}"


def _sandbox_inventory_text(offer: Mapping[str, Any]) -> str:
    return (
        f"demo_price_vnd: {offer['price_vnd']} VND\n"
        f"demo_stock: {offer['available_quantity']} item\n"
        f"demo_offer_version: {offer['version']}"
    )


def _sandbox_tool_id(prefix: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(canonical).hexdigest()}"


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
