"""Focused contract tests for Package 8 immutable evidence resolution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.evaluation.benchmark_evidence import (
    CatalogReviewAuthorityUnavailableErrorV3,
    CatalogReviewAuthorizationMismatchErrorV3,
    CatalogReviewResolvedEvidenceV3,
    EvidenceMetadataUnavailableErrorV3,
    EvidenceProvenanceMismatchErrorV3,
    ImmutableBenchmarkEvidenceResolverV3,
    ImmutableCatalogReviewSourceV3,
    SandboxEvidenceAuthorityUnavailableErrorV3,
    SandboxEvidenceAuthorizationMismatchErrorV3,
    SandboxEvidenceProvenanceMismatchErrorV3,
    SandboxResolvedEvidenceV3,
    UnsafeSynchronousEvidenceResolutionErrorV3,
)
from app.evaluation.benchmark_reporting import (
    EvidenceBindingKeyV3,
    ResolvedExactEvidenceV3,
)
from app.evaluation.protocol import canonical_json_bytes
from app.evaluation.v3_gold import SourceAssetsV3
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
SANDBOX_NAMESPACE_ID = "namespace_" + "1" * 64
SANDBOX_FIXTURE_ID = "sandbox_held_shopping_merchant_02"
SANDBOX_FIXTURE_OFFER_ID = "offer_held_shopping_merchant_02"
SANDBOX_SNAPSHOT_VERSION_ID = "inventory_held_shopping_merchant_02"
SANDBOX_SOURCE_VERSION_ID = "cat_" + "2" * 64
SANDBOX_CART_FIXTURE_ID = "sandbox_held_shopping_merchant_01"
SANDBOX_FIXTURE_CART_ID = "cart_held_shopping_merchant_01"


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


class _SandboxSource:
    def __init__(self, evidence: SandboxResolvedEvidenceV3 | None) -> None:
        self.evidence = evidence
        self.calls: list[EvidenceBindingKeyV3] = []

    def resolve(
        self, binding: EvidenceBindingKeyV3
    ) -> SandboxResolvedEvidenceV3 | None:
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
            authorization=_authorization(),
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


def _public_asset_bindings() -> tuple[Path, SourceAssetsV3]:
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "evaluation/v3/gold.v3.json").read_text("utf-8"))
    return root, SourceAssetsV3.model_validate(document["source_assets"])


def _public_runtime_evidence(kind: EvidenceKind, *, product_id: int = 109):
    """Use the production read tool against source-derived ORM rows, without I/O."""
    from app.data.contracts import DatasetManifest, NormalizedProduct, NormalizedReview
    from app.models.dataset_source import DatasetSource
    from app.models.product import Product
    from app.models.review import Review
    from app.v2.tools import CatalogSnapshot, V2ReadTools

    root, bindings = _public_asset_bindings()
    manifest = DatasetManifest.model_validate_json(
        (root / bindings.snapshot_manifest.path).read_bytes()
    )
    normalized = NormalizedProduct.model_validate_json(
        (root / bindings.products.path).read_text("utf-8").splitlines()[product_id - 1]
    )
    product = Product(
        id=product_id,
        source_id=1,
        platform="Tiki",
        name=normalized.name,
        authors=normalized.authors,
        publisher=normalized.publisher,
        category=normalized.category,
        price=normalized.price_vnd,
        rating=normalized.rating,
        page_count=normalized.page_count,
        source_review_count=normalized.source_review_count,
        dataset_source=DatasetSource(id=1, retrieved_at=manifest.retrieved_at),
    )
    reviews = [
        Review(id=index, rating=row.rating, content=row.content)
        for index, line in enumerate(
            (root / bindings.reviews.path).read_text("utf-8").splitlines(), start=1
        )
        if (row := NormalizedReview.model_validate_json(line)).product_external_id
        == normalized.external_id
    ]

    def forbidden_session():
        raise AssertionError("offline evidence must not open a database")

    class Repository:
        def get_products_by_ids(self, ids):
            assert ids == (product_id,)
            return [product]

        def get_product_reviews(self, *, product_id, limit):
            assert limit == 20
            return product, reviews[:limit]

    snapshot = CatalogSnapshot(
        version_id=SANDBOX_SOURCE_VERSION_ID,
        observed_at=manifest.retrieved_at,
        source_ids=(1,),
    )
    tools = V2ReadTools(forbidden_session, catalog_snapshot=snapshot)
    if kind is EvidenceKind.CATALOG:
        _, evidence = tools._catalog([product])
    else:
        _, evidence = tools._reviews(Repository(), (product_id,), trust=False)
    return evidence.references[0], evidence.excerpts[0].exact_text


@pytest.mark.parametrize("kind", (EvidenceKind.CATALOG, EvidenceKind.REVIEW))
def test_public_asset_resolution_matches_production_tool_record(
    kind: EvidenceKind,
) -> None:
    root, bindings = _public_asset_bindings()
    source = ImmutableCatalogReviewSourceV3(
        project_root=root,
        bindings=bindings,
        snapshot_version_id=SANDBOX_SOURCE_VERSION_ID,
    )
    reference, exact_text = _public_runtime_evidence(kind)
    resolved = source.reopen(reference, _authorization())
    assert resolved.exact_text == exact_text
    assert resolved.reference.kind is kind
    assert resolved.authorization == _authorization()
    assert resolved.content_sha256 == sha256_utf8(exact_text)


@pytest.mark.parametrize(
    "field,value",
    (
        ("source_id", "catalog_product_108"),
        ("source_version_id", "cat_" + "3" * 64),
        ("chunk_id", "tch_" + "4" * 64),
        ("span_id", "tsp_" + "5" * 64),
        ("evidence_id", "evidence_foreign"),
        ("title", "A forged answer-derived title"),
        ("observed_at", OBSERVED_AT),
    ),
)
def test_public_asset_resolution_rejects_changed_binding(
    field: str, value: object
) -> None:
    root, bindings = _public_asset_bindings()
    source = ImmutableCatalogReviewSourceV3(
        project_root=root,
        bindings=bindings,
        snapshot_version_id=SANDBOX_SOURCE_VERSION_ID,
    )
    reference, _ = _public_runtime_evidence(EvidenceKind.CATALOG)
    with pytest.raises(EvidenceProvenanceMismatchErrorV3):
        source.reopen(reference.model_copy(update={field: value}), _authorization())


@pytest.mark.parametrize("asset", ("products", "reviews", "snapshot_manifest"))
def test_public_asset_resolution_rejects_changed_asset_hash(
    tmp_path: Path, asset: str
) -> None:
    root, bindings = _public_asset_bindings()
    for name in ("products", "reviews", "snapshot_manifest"):
        binding = getattr(bindings, name)
        target = tmp_path / binding.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            (root / binding.path).read_bytes() + (b" " if asset == name else b"")
        )
    with pytest.raises(EvidenceProvenanceMismatchErrorV3, match="hash mismatch"):
        ImmutableCatalogReviewSourceV3(
            project_root=tmp_path,
            bindings=bindings,
            snapshot_version_id=SANDBOX_SOURCE_VERSION_ID,
        )


def test_public_asset_resolution_refuses_rank_without_candidate_authority() -> None:
    root, bindings = _public_asset_bindings()
    source = ImmutableCatalogReviewSourceV3(
        project_root=root,
        bindings=bindings,
        snapshot_version_id=SANDBOX_SOURCE_VERSION_ID,
    )
    reference, _ = _public_runtime_evidence(EvidenceKind.CATALOG)
    reference = reference.model_copy(update={"source_id": "rank_109_" + "a" * 24})
    with pytest.raises(CatalogReviewAuthorityUnavailableErrorV3):
        source.reopen(reference, _authorization())


@pytest.mark.parametrize("change", ("tenant", "principal", "mode", "scope", "store"))
def test_public_asset_exact_text_cannot_be_reused_under_foreign_authority(
    change: str,
) -> None:
    root, bindings = _public_asset_bindings()
    source = ImmutableCatalogReviewSourceV3(
        project_root=root,
        bindings=bindings,
        snapshot_version_id=SANDBOX_SOURCE_VERSION_ID,
    )
    reference, _ = _public_runtime_evidence(EvidenceKind.CATALOG)
    authority = source.reopen(reference, _authorization())
    access = _authorization()
    updates = {
        "tenant": {"tenant_id": "tenant_other"},
        "principal": {"principal_id": "principal_other"},
        "mode": {"mode": ConversationMode.MERCHANT},
        "store": {"store_id": "foreign_store"},
    }
    access = access.model_copy(
        update={"scopes": frozenset()}
        if change == "scope"
        else {"binding": access.binding.model_copy(update=updates[change])}
    )
    resolver = _resolver(
        _FakeKnowledgeService(None),
        authorization=access,
        reference_source=_ReferenceSource(reference),
        catalog_review_source=_CatalogReviewSource(authority),
    )
    with pytest.raises(CatalogReviewAuthorizationMismatchErrorV3):
        resolver.resolve(authority.binding)


@pytest.mark.parametrize("kind", (EvidenceKind.CATALOG, EvidenceKind.REVIEW))
def test_public_asset_reopen_requires_current_read_scope(kind: EvidenceKind) -> None:
    root, bindings = _public_asset_bindings()
    source = ImmutableCatalogReviewSourceV3(
        project_root=root,
        bindings=bindings,
        snapshot_version_id=SANDBOX_SOURCE_VERSION_ID,
    )
    reference, _ = _public_runtime_evidence(kind)
    with pytest.raises(CatalogReviewAuthorizationMismatchErrorV3):
        source.reopen(
            reference, _authorization().model_copy(update={"scopes": frozenset()})
        )


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


def test_sandbox_inventory_resolution_uses_exact_receipt_bound_fixture() -> None:
    binding, reference, authority = _sandbox_authority()
    source = _SandboxSource(authority)
    service = _FakeKnowledgeService(None)
    resolver = _resolver(
        service,
        authorization=authority.authorization,
        reference_source=_ReferenceSource(reference),
        sandbox_source=source,
    )

    first = resolver.resolve(binding)
    second = resolver.resolve(binding)

    assert first == second
    assert first.binding == binding
    assert first.exact_text == authority.exact_text
    assert source.calls == [binding, binding]
    assert service.calls == []


def test_sandbox_inventory_without_explicit_authority_fails_closed() -> None:
    binding, reference, authority = _sandbox_authority()
    resolver = _resolver(
        _FakeKnowledgeService(None),
        authorization=authority.authorization,
        reference_source=_ReferenceSource(reference),
    )

    with pytest.raises(SandboxEvidenceAuthorityUnavailableErrorV3):
        resolver.resolve(binding)


@pytest.mark.parametrize(
    ("update", "message"),
    (
        ({"fixture_id": "sandbox_other_fixture"}, "fixture identity"),
        ({"fixture_sha256": "0" * 64}, "fixture hash"),
        ({"fixture_offer_id": "offer_other_fixture"}, "bound offer"),
        ({"offer_id": "offer_" + "3" * 48}, "execution namespace"),
        ({"offer_version": 2}, "offer version"),
        ({"subject_id": "offer_other_subject"}, "subject"),
    ),
)
def test_sandbox_inventory_rejects_fixture_offer_and_subject_mismatches(
    update: dict[str, object],
    message: str,
) -> None:
    binding, reference, authority = _sandbox_authority()
    tampered = authority.model_copy(update=update)
    resolver = _resolver(
        _FakeKnowledgeService(None),
        authorization=authority.authorization,
        reference_source=_ReferenceSource(reference),
        sandbox_source=_SandboxSource(tampered),
    )

    with pytest.raises(SandboxEvidenceProvenanceMismatchErrorV3, match=message):
        resolver.resolve(binding)


@pytest.mark.parametrize(
    "authorization",
    (
        lambda: _merchant_authorization(tenant_id="tenant_other"),
        lambda: _merchant_authorization(principal_id="principal_other"),
    ),
)
def test_sandbox_inventory_rejects_foreign_tenant_or_principal(
    authorization,
) -> None:
    binding, reference, authority = _sandbox_authority()
    resolver = _resolver(
        _FakeKnowledgeService(None),
        authorization=authorization(),
        reference_source=_ReferenceSource(reference),
        sandbox_source=_SandboxSource(authority),
    )

    with pytest.raises(SandboxEvidenceAuthorizationMismatchErrorV3):
        resolver.resolve(binding)


@pytest.mark.parametrize(
    ("record_kind", "expected_text"),
    (
        (
            "shopper_cart_summary",
            "demo_cart_version: 1\ndemo_cart_total_vnd: 240000 VND",
        ),
        (
            "shopper_cart_item",
            "demo_price_vnd: 120000 VND\ndemo_quantity: 2 item\n"
            "demo_line_total_vnd: 240000 VND",
        ),
        (
            "shopper_checkout_summary",
            "demo_checkout_can_checkout: True\ndemo_checkout_issues: none\n"
            "demo_cart_version: 1\ndemo_cart_total_vnd: 240000 VND",
        ),
        (
            "shopper_checkout_item",
            "demo_price_vnd: 120000 VND\ndemo_quantity: 2 item\n"
            "demo_line_total_vnd: 240000 VND",
        ),
    ),
)
def test_sandbox_cart_and_checkout_records_resolve_exact_fixture_text(
    record_kind: str,
    expected_text: str,
) -> None:
    binding, reference, authority = _sandbox_cart_authority(record_kind)
    resolver = _resolver(
        _FakeKnowledgeService(None),
        authorization=authority.authorization,
        reference_source=_ReferenceSource(reference),
        sandbox_source=_SandboxSource(authority),
    )

    resolved = resolver.resolve(binding)

    assert expected_text in resolved.exact_text
    assert resolved.exact_text == authority.exact_text


@pytest.mark.parametrize("field", ("source_id", "span_id"))
def test_sandbox_cart_rejects_source_and_span_tampering(field: str) -> None:
    binding, _, authority = _sandbox_cart_authority("shopper_cart_item")
    updates = {
        field: ("sandbox_cart_foreign" if field == "source_id" else "tsp_" + "9" * 64)
    }
    tampered_binding = binding.model_copy(update=updates)
    tampered_binding = tampered_binding.model_copy(
        update={
            "evidence_id": stable_evidence_id(
                source_id=tampered_binding.source_id,
                source_version_id=tampered_binding.source_version_id,
                chunk_id=tampered_binding.chunk_id,
                span_id=tampered_binding.span_id,
            )
        }
    )
    tampered_reference = authority.reference.model_copy(
        update=tampered_binding.model_dump()
    )
    tampered_authority = authority.model_copy(
        update={"binding": tampered_binding, "reference": tampered_reference}
    )
    resolver = _resolver(
        _FakeKnowledgeService(None),
        authorization=authority.authorization,
        reference_source=_ReferenceSource(tampered_reference),
        sandbox_source=_SandboxSource(tampered_authority),
    )

    with pytest.raises(SandboxEvidenceProvenanceMismatchErrorV3):
        resolver.resolve(tampered_binding)


def test_sandbox_checkout_rejects_action_execution_fixture() -> None:
    binding, reference, authority = _sandbox_cart_authority("shopper_checkout_summary")
    payload = json.loads(authority.fixture_payload_json)
    payload["target_capability_id"] = "shopper.checkout.execute"
    payload["proposal_parameters"] = None
    payload_json = canonical_json_bytes(payload).decode("utf-8")
    tampered = authority.model_copy(
        update={
            "fixture_payload_json": payload_json,
            "fixture_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        }
    )
    resolver = _resolver(
        _FakeKnowledgeService(None),
        authorization=authority.authorization,
        reference_source=_ReferenceSource(reference),
        sandbox_source=_SandboxSource(tampered),
    )

    with pytest.raises(
        SandboxEvidenceProvenanceMismatchErrorV3,
        match="checkout preview",
    ):
        resolver.resolve(binding)


def _resolver(
    service: _FakeKnowledgeService,
    *,
    authorization: ResourceAuthorization | None = None,
    reference_source: _ReferenceSource,
    catalog_review_source: _CatalogReviewSource | None = None,
    sandbox_source: _SandboxSource | None = None,
    coroutine_runner=None,
) -> ImmutableBenchmarkEvidenceResolverV3:
    return ImmutableBenchmarkEvidenceResolverV3(
        service,
        authorization=authorization or _authorization(),
        corpus_version_id=CORPUS_VERSION_ID,
        index_manifest_id=INDEX_MANIFEST_ID,
        reference_source=reference_source,
        catalog_review_source=catalog_review_source,
        sandbox_source=sandbox_source,
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


def _merchant_authorization(
    *,
    tenant_id: str = "tenant_primary",
    principal_id: str = "principal_primary",
) -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode=ConversationMode.MERCHANT,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read", "merchant.read"}),
    )


def _shopper_authorization() -> ResourceAuthorization:
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


def _sandbox_authority() -> tuple[
    EvidenceBindingKeyV3,
    EvidenceReference,
    SandboxResolvedEvidenceV3,
]:
    authorization = _merchant_authorization()
    fixture_payload = {
        "fixture_id": SANDBOX_FIXTURE_ID,
        "reset_revision": 1,
        "cart": None,
        "merchant": {
            "snapshot_version_id": SANDBOX_SNAPSHOT_VERSION_ID,
            "offers": [
                {
                    "offer_id": SANDBOX_FIXTURE_OFFER_ID,
                    "product_id": 7,
                    "price_vnd": 120_000,
                    "available_quantity": 9,
                    "version": 1,
                }
            ],
        },
        "target_capability_id": "merchant.offer.propose",
        "proposal_parameters": {
            "kind": "offer_proposal",
            "capability_id": "merchant.offer.propose",
            "offer_id": SANDBOX_FIXTURE_OFFER_ID,
            "expected_version": 1,
            "new_price_vnd": 125_000,
        },
        "confirmed_proposal_id": None,
    }
    fixture_payload_json = canonical_json_bytes(fixture_payload).decode("utf-8")
    fixture_sha256 = hashlib.sha256(fixture_payload_json.encode("utf-8")).hexdigest()
    offer_id = (
        "offer_"
        + hashlib.sha256(
            f"{SANDBOX_NAMESPACE_ID}:{SANDBOX_FIXTURE_OFFER_ID}".encode("utf-8")
        ).hexdigest()[:48]
    )
    exact_text = "demo_price_vnd: 120000 VND\ndemo_stock: 9 item\ndemo_offer_version: 1"
    source_id = f"sandbox_inventory_{offer_id}"
    chunk_id = _test_tool_id(
        "tch",
        {
            "source": source_id,
            "version": SANDBOX_SOURCE_VERSION_ID,
            "text": exact_text,
        },
    )
    span_id = _test_tool_id(
        "tsp",
        {"chunk": chunk_id, "start": 0, "end": len(exact_text)},
    )
    binding = EvidenceBindingKeyV3(
        evidence_id=stable_evidence_id(
            source_id=source_id,
            source_version_id=SANDBOX_SOURCE_VERSION_ID,
            chunk_id=chunk_id,
            span_id=span_id,
        ),
        source_id=source_id,
        source_version_id=SANDBOX_SOURCE_VERSION_ID,
        chunk_id=chunk_id,
        span_id=span_id,
    )
    reference = EvidenceReference(
        **binding.model_dump(),
        display_label="[C1]",
        kind=EvidenceKind.SANDBOX,
        title=f"Demo inventory — offer {offer_id}",
        observed_at=OBSERVED_AT,
    )
    authority = SandboxResolvedEvidenceV3(
        binding=binding,
        reference=reference,
        authorization=authorization,
        fixture_id=SANDBOX_FIXTURE_ID,
        fixture_sha256=fixture_sha256,
        reset_revision=1,
        fixture_payload_json=fixture_payload_json,
        execution_namespace_id=SANDBOX_NAMESPACE_ID,
        snapshot_version_id=SANDBOX_SNAPSHOT_VERSION_ID,
        fixture_offer_id=SANDBOX_FIXTURE_OFFER_ID,
        offer_id=offer_id,
        offer_version=1,
        subject_id=f"offer_{offer_id}",
        exact_text=exact_text,
        content_sha256=sha256_utf8(exact_text),
    )
    return binding, reference, authority


def _test_tool_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


def _sandbox_cart_authority(
    record_kind: str,
) -> tuple[EvidenceBindingKeyV3, EvidenceReference, SandboxResolvedEvidenceV3]:
    authorization = _shopper_authorization()
    fixture_payload = {
        "fixture_id": SANDBOX_CART_FIXTURE_ID,
        "reset_revision": 1,
        "cart": {
            "cart_id": SANDBOX_FIXTURE_CART_ID,
            "version": 1,
            "lines": [
                {
                    "product_id": 7,
                    "quantity": 2,
                    "unit_price_vnd": 120_000,
                }
            ],
        },
        "merchant": None,
        "target_capability_id": "shopper.checkout.propose",
        "proposal_parameters": {
            "kind": "checkout_proposal",
            "capability_id": "shopper.checkout.propose",
            "cart_id": SANDBOX_FIXTURE_CART_ID,
            "expected_version": 1,
        },
        "confirmed_proposal_id": None,
    }
    payload_json = canonical_json_bytes(fixture_payload).decode("utf-8")
    cart_id = (
        "cart_"
        + hashlib.sha256(
            f"{SANDBOX_NAMESPACE_ID}:{SANDBOX_FIXTURE_CART_ID}".encode("utf-8")
        ).hexdigest()[:48]
    )
    is_checkout = record_kind.startswith("shopper_checkout_")
    is_item = record_kind.endswith("_item")
    prefix = "sandbox_checkout" if is_checkout else "sandbox_cart"
    if is_item:
        source_id = f"{prefix}_{cart_id}_7"
        subject_id = "product_7"
        title_prefix = "Demo checkout" if is_checkout else "Demo cart"
        title = f"{title_prefix} item — product 7 in {cart_id}"
        exact_text = (
            "demo_price_vnd: 120000 VND\n"
            "demo_quantity: 2 item\n"
            "demo_line_total_vnd: 240000 VND"
        )
        product_id = 7
    elif is_checkout:
        source_id = f"sandbox_checkout_{cart_id}"
        subject_id = cart_id
        title = f"Demo checkout preview — {cart_id}"
        exact_text = (
            "demo_checkout_can_checkout: True\n"
            "demo_checkout_issues: none\n"
            "demo_cart_version: 1\n"
            "demo_cart_total_vnd: 240000 VND"
        )
        product_id = None
    else:
        source_id = f"sandbox_cart_{cart_id}"
        subject_id = cart_id
        title = f"Demo cart — {cart_id}"
        exact_text = (
            f"demo_cart_id: {cart_id}\n"
            "demo_cart_version: 1\n"
            "demo_cart_total_vnd: 240000 VND"
        )
        product_id = None
    chunk_id = _test_tool_id(
        "tch",
        {
            "source": source_id,
            "version": SANDBOX_SOURCE_VERSION_ID,
            "text": exact_text,
        },
    )
    span_id = _test_tool_id(
        "tsp", {"chunk": chunk_id, "start": 0, "end": len(exact_text)}
    )
    binding = EvidenceBindingKeyV3(
        evidence_id=stable_evidence_id(
            source_id=source_id,
            source_version_id=SANDBOX_SOURCE_VERSION_ID,
            chunk_id=chunk_id,
            span_id=span_id,
        ),
        source_id=source_id,
        source_version_id=SANDBOX_SOURCE_VERSION_ID,
        chunk_id=chunk_id,
        span_id=span_id,
    )
    reference = EvidenceReference(
        **binding.model_dump(),
        display_label="[C1]",
        kind=EvidenceKind.SANDBOX,
        title=title,
        observed_at=OBSERVED_AT,
    )
    authority = SandboxResolvedEvidenceV3(
        binding=binding,
        reference=reference,
        authorization=authorization,
        fixture_id=SANDBOX_CART_FIXTURE_ID,
        fixture_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        reset_revision=1,
        fixture_payload_json=payload_json,
        execution_namespace_id=SANDBOX_NAMESPACE_ID,
        record_kind=record_kind,  # type: ignore[arg-type]
        fixture_cart_id=SANDBOX_FIXTURE_CART_ID,
        cart_id=cart_id,
        product_id=product_id,
        subject_id=subject_id,
        exact_text=exact_text,
        content_sha256=sha256_utf8(exact_text),
    )
    return binding, reference, authority
