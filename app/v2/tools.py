"""Bounded historical read tools shared by every v2 orchestration variant.

These adapters perform deterministic database reads and pure existing heuristics.
Expert model reasoning belongs to the supervisor, never to this shared tool layer.
Sandbox offers and actions are separate Package 5 services.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeAlias

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agents.product.skills import rank_products
from app.agents.review.skills import extract_review_aspects
from app.agents.trust.skills import analyze_review_trust, detect_complaints
from app.contracts import TaskStatus
from app.knowledge.service import KnowledgeService
from app.knowledge.v2_contracts import (
    PublishedKnowledgeSnapshot,
    ResolvedKnowledgeEvidence,
    stable_evidence_id,
)
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.repositories.ecommerce import EcommerceRepository, product_comparison_fact
from app.v2.authorization import (
    DEMO_STORE_ID,
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceNotFoundError,
    required_scopes_for_mode,
)
from app.v2.contracts import (
    EvidenceKind,
    EvidenceReference,
    SafeExecutionError,
    V2Contract,
)
from app.v2.registry import (
    CapabilityEffect,
    CatalogSearchInput,
    KnowledgeRetrieveInput,
    MarketMetric,
    MarketResult,
    MarketSnapshotInput,
    MerchantReadInput,
    ProductCandidate,
    ProductResult,
    ProductSelectionInput,
    ReviewFinding,
    ReviewResult,
    TrustFinding,
    TrustResult,
    V2CapabilityRegistry,
    build_default_v2_registry,
)
from app.v2.runtime_contracts import (
    EvidenceExcerpt,
    ExpertResult,
    RuntimeOperation,
    StructuredFact,
    ToolEvidence,
)

SessionFactory: TypeAlias = Callable[[], Session]
FactEntry: TypeAlias = tuple[str, str | int | Decimal | bool, str | None]
REVIEW_SAMPLE_LIMIT = 20
MARKET_EVIDENCE_BUCKET_LIMIT = 8


class ReadToolError(RuntimeError):
    """A safe deterministic error; the exception never includes raw database text."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Identity of the immutable imported sources used by historical reads."""

    version_id: str
    observed_at: datetime
    source_ids: tuple[int, ...]


def load_catalog_snapshot(session_factory: SessionFactory) -> CatalogSnapshot:
    """Read only source identity/checksums, without loading a raw dataset."""
    with session_factory() as session:
        return _catalog_snapshot(session)


def _catalog_snapshot(session: Session) -> CatalogSnapshot:
    sources = tuple(session.scalars(select(DatasetSource).order_by(DatasetSource.id)))
    if not sources:
        raise ReadToolError("catalog_snapshot_unavailable")
    identity = [
        {
            "source_id": source.id,
            "dataset_id": source.dataset_id,
            "dataset_version": source.dataset_version,
            "profile": source.profile,
            "snapshot_sha256": source.snapshot_sha256,
            "products_sha256": source.products_sha256,
            "reviews_sha256": source.reviews_sha256,
            "retrieved_at": _aware(source.retrieved_at).isoformat(),
        }
        for source in sources
    ]
    return CatalogSnapshot(
        version_id=_tool_id("cat", identity),
        observed_at=max(_aware(source.retrieved_at) for source in sources),
        source_ids=tuple(source.id for source in sources),
    )


class V2ReadTools:
    """Typed, currently authorized, version-bound reads with exact tool evidence."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        catalog_snapshot: CatalogSnapshot,
        knowledge_service: KnowledgeService | None = None,
        knowledge_snapshot: PublishedKnowledgeSnapshot | None = None,
        registry: V2CapabilityRegistry | None = None,
    ) -> None:
        if (knowledge_service is None) != (knowledge_snapshot is None):
            raise ValueError(
                "knowledge service and pinned snapshot must be supplied together"
            )
        self.session_factory = session_factory
        self.catalog_snapshot = catalog_snapshot
        self.knowledge_service = knowledge_service
        self.knowledge_snapshot = knowledge_snapshot
        self.registry = registry or build_default_v2_registry()

    def data_version_ids(self, capability: str) -> tuple[str, ...]:
        if capability == "knowledge.retrieve":
            if self.knowledge_snapshot is None:
                raise ReadToolError("knowledge_unavailable")
            return (
                self.catalog_snapshot.version_id,
                self.knowledge_snapshot.corpus_version_id,
                self.knowledge_snapshot.index_manifest_id,
            )
        return (self.catalog_snapshot.version_id,)

    async def execute(
        self, operation: RuntimeOperation, access: ResourceAuthorization
    ) -> ExpertResult:
        # Frozen Pydantic objects can still contain mutable dictionaries. Rebuild
        # a detached operation and verify its key before crossing any I/O boundary.
        operation = RuntimeOperation.model_validate(operation.model_dump(mode="python"))
        definition = self.registry.capability(operation.capability)
        if access.binding.store_id != DEMO_STORE_ID:
            raise ResourceNotFoundError
        if (
            access.binding.mode not in definition.allowed_modes
            or not required_scopes_for_mode(access.binding.mode) <= access.scopes
            or not definition.required_permissions <= access.scopes
        ):
            raise AuthorizationDeniedError
        started_at = datetime.now(UTC)
        try:
            if definition.effect != CapabilityEffect.READ:
                raise ReadToolError("read_tool_write_forbidden")
            if operation.service != definition.service:
                raise ReadToolError("tool_service_mismatch")
            request = definition.validate_input(operation.parameters)
            if request.model_dump(mode="json") != operation.parameters:
                raise ReadToolError("tool_parameters_not_canonical")
            if set(operation.data_version_ids) != set(
                self.data_version_ids(operation.capability)
            ):
                raise ReadToolError("stale_data_version")
            if isinstance(request, KnowledgeRetrieveInput):
                output, evidence = await self._knowledge(request, access)
            else:
                output, evidence = await asyncio.to_thread(
                    self._read, operation.capability, request
                )
            definition.validate_output(output.model_dump(mode="json"))
        except ReadToolError as exc:
            error = SafeExecutionError(
                code=exc.code,
                message="Không thể hoàn thành bước đọc với dữ liệu hiện tại.",
                retryable=exc.retryable,
            )
        except ValidationError:
            error = SafeExecutionError(
                code="tool_contract_invalid",
                message="Tham số hoặc kết quả công cụ không hợp lệ.",
            )
        except SQLAlchemyError:
            error = SafeExecutionError(
                code="read_database_unavailable",
                message="Nguồn dữ liệu tạm thời không khả dụng.",
                retryable=True,
            )
        else:
            return ExpertResult(
                operation=operation,
                status=TaskStatus.SUCCESS,
                output=output.model_dump(mode="json"),
                evidence=evidence,
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )
        return ExpertResult(
            operation=operation,
            status=TaskStatus.FAILED,
            error=error,
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def _guard_snapshot(self, session: Session) -> None:
        if _catalog_snapshot(session) != self.catalog_snapshot:
            raise ReadToolError("stale_data_version")

    def _read(
        self, capability: str, request: V2Contract
    ) -> tuple[V2Contract, ToolEvidence]:
        with self.session_factory() as session:
            self._guard_snapshot(session)
            repository = EcommerceRepository(session)
            if capability in {"product.catalog.search", "product.rank"}:
                assert isinstance(request, CatalogSearchInput)
                rows = repository.search_products(
                    query=request.query,
                    author=request.author,
                    category=request.category,
                    publisher=request.publisher,
                    min_price=request.min_price_vnd,
                    max_price=request.max_price_vnd,
                    limit=request.candidate_limit,
                )
                product_ids = tuple(int(str(row["id"])) for row in rows)
                products = self._products(repository, product_ids)
                return self._catalog(products, ranked=capability == "product.rank")
            if capability == "product.compare":
                assert isinstance(request, ProductSelectionInput)
                return self._catalog(self._products(repository, request.product_ids))
            if capability == "merchant.catalog.read":
                assert isinstance(request, MerchantReadInput)
                ids = request.product_ids
                if not ids:
                    rows = repository.search_products(limit=5)
                    ids = tuple(int(str(row["id"])) for row in rows)
                return self._catalog(self._products(repository, ids))
            if capability in {
                "review.retrieve",
                "review.compare",
                "trust.analyze",
                "trust.compare",
            }:
                assert isinstance(request, ProductSelectionInput)
                return self._reviews(
                    repository,
                    request.product_ids,
                    trust=capability.startswith("trust."),
                )
            if capability == "market.snapshot":
                assert isinstance(request, MarketSnapshotInput)
                return self._market(repository, request)
            raise ReadToolError("read_capability_unavailable")

    def _products(
        self, repository: EcommerceRepository, ids: tuple[int, ...]
    ) -> list[Product]:
        products = repository.get_products_by_ids(ids)
        if len(products) != len(ids) or any(
            product.platform != "Tiki"
            or product.source_id not in self.catalog_snapshot.source_ids
            for product in products
        ):
            raise ReadToolError("catalog_product_not_found")
        return products

    def _catalog(
        self, products: list[Product], *, ranked: bool = False
    ) -> tuple[ProductResult, ToolEvidence]:
        ranking: dict[int, Decimal] = {}
        if ranked:
            ranked_rows = rank_products(
                [product_comparison_fact(product) for product in products]
            )["products"]
            positions = {int(row["id"]): index for index, row in enumerate(ranked_rows)}
            ranking = {
                int(row["id"]): Decimal(str(row["ranking_score"]))
                for row in ranked_rows
            }
            products = sorted(products, key=lambda product: positions[product.id])
        builder = _EvidenceBuilder(self.catalog_snapshot)
        candidates: list[ProductCandidate] = []
        for product in products:
            author = "; ".join(product.authors) or None
            candidates.append(
                ProductCandidate(
                    product_id=product.id,
                    title=product.name,
                    author=author
                    if author is not None and len(author) <= 160
                    else None,
                    price_vnd=product.price if product.price > 0 else None,
                    rating=product.rating,
                    catalog_version_id=self.catalog_snapshot.version_id,
                )
            )
            entries: list[FactEntry] = [
                ("title", product.name, None),
                ("category", product.category, None),
                ("snapshot_price_vnd", product.price, "VND"),
                ("snapshot_review_count", product.source_review_count, "review"),
            ]
            if author:
                entries.append(("author", author, None))
            if product.publisher:
                entries.append(("publisher", product.publisher, None))
            if product.page_count is not None:
                entries.append(("page_count", product.page_count, "page"))
            if product.rating is not None:
                entries.append(
                    ("snapshot_rating", Decimal(str(product.rating)), "rating_5")
                )
            builder.add(
                source_id=f"catalog_product_{product.id}",
                subject_id=f"product_{product.id}",
                title=f"{product.name} — dữ liệu catalog lịch sử",
                kind=EvidenceKind.CATALOG,
                entries=entries,
                observed_at=_product_observed_at(product),
            )
            if ranked:
                candidate_scope = _tool_id("rnk", sorted(ranking))
                builder.add(
                    source_id=f"rank_{product.id}_{candidate_scope[-24:]}",
                    subject_id=f"product_{product.id}",
                    title=(
                        f"{product.name} — điểm xếp hạng snapshot trong nhóm ứng viên"
                    ),
                    kind=EvidenceKind.CATALOG,
                    entries=[("ranking_score", ranking[product.id], "score")],
                )
        evidence = builder.build()
        return ProductResult(
            products=tuple(candidates), evidence=evidence.references
        ), evidence

    def _reviews(
        self, repository: EcommerceRepository, ids: tuple[int, ...], *, trust: bool
    ) -> tuple[V2Contract, ToolEvidence]:
        products = self._products(repository, ids)
        builder = _EvidenceBuilder(self.catalog_snapshot)
        review_findings: list[ReviewFinding] = []
        trust_findings: list[TrustFinding] = []
        for product in products:
            _, reviews = repository.get_product_reviews(
                product_id=product.id, limit=REVIEW_SAMPLE_LIMIT
            )
            sample = [
                {"id": row.id, "rating": row.rating, "content": row.content}
                for row in reviews
            ]
            entries: list[FactEntry] = [("sampled_review_count", len(sample), "review")]
            if trust:
                complaints = detect_complaints(sample)
                signals = analyze_review_trust(sample)
                count = int(complaints["complaint_count"])
                limitation = str(signals["limitation"])
                entries.extend(
                    [
                        ("complaint_count", count, "complaint"),
                        (
                            "flagged_review_count",
                            int(signals["flagged_text_quality_count"]),
                            "review",
                        ),
                        ("trust_limitation", limitation, None),
                    ]
                )
                evidence_id = builder.add(
                    source_id=f"trust_sample_{product.id}",
                    subject_id=f"product_{product.id}",
                    title=(
                        f"{product.name} — heuristic trên tối đa "
                        f"{REVIEW_SAMPLE_LIMIT} review snapshot"
                    ),
                    kind=EvidenceKind.TRUST,
                    entries=entries,
                    observed_at=_product_observed_at(product),
                )
                trust_findings.append(
                    TrustFinding(
                        product_id=product.id,
                        complaint_count=count,
                        summary=(
                            f"{count} tín hiệu phàn nàn "
                            f"trong mẫu {len(sample)} review. "
                            f"{limitation}"
                        ),
                        evidence_ids=(evidence_id,),
                    )
                )
            else:
                average = (
                    (
                        sum((Decimal(row.rating) for row in reviews), Decimal(0))
                        / len(reviews)
                    ).quantize(Decimal("0.001"))
                    if reviews
                    else None
                )
                if average is not None:
                    entries.append(("sampled_average_rating", average, "rating_5"))
                aspects = extract_review_aspects(sample)["aspects"]
                if aspects:
                    aspect_text = "; ".join(
                        f"{item['name']}: {item['mentions']} lượt đề cập, "
                        f"{item['negative_mentions']} tín hiệu tiêu cực"
                        for item in aspects[:4]
                    )
                    entries.append(("review_aspects", aspect_text, None))
                evidence_id = builder.add(
                    source_id=f"review_sample_{product.id}",
                    subject_id=f"product_{product.id}",
                    title=(
                        f"{product.name} — mẫu tối đa "
                        f"{REVIEW_SAMPLE_LIMIT} review lịch sử"
                    ),
                    kind=EvidenceKind.REVIEW,
                    entries=entries,
                    observed_at=_product_observed_at(product),
                )
                review_findings.append(
                    ReviewFinding(
                        product_id=product.id,
                        review_count=len(sample),
                        average_rating=float(average) if average is not None else None,
                        summary=(
                            f"Mẫu {len(sample)} review lịch sử; "
                            "thống kê không đại diện cho mọi người mua."
                        ),
                        evidence_ids=(evidence_id,),
                    )
                )
        result: V2Contract = (
            TrustResult(findings=tuple(trust_findings))
            if trust
            else ReviewResult(findings=tuple(review_findings))
        )
        return result, builder.build()

    def _market(
        self, repository: EcommerceRepository, request: MarketSnapshotInput
    ) -> tuple[MarketResult, ToolEvidence]:
        statistics = repository.get_product_statistics(
            category=request.category, author=request.author
        )
        distribution = statistics[f"{request.dimension.value}_distribution"]
        product_count = statistics["product_count"]
        if not isinstance(distribution, dict) or type(product_count) is not int:
            raise ReadToolError("market_snapshot_invalid")
        metrics = tuple(
            MarketMetric(label=label, count=count)
            for label, count in sorted(distribution.items())
        )
        builder = _EvidenceBuilder(self.catalog_snapshot)
        scope = _tool_id("mkt", request.model_dump(mode="json"))[-24:]
        builder.add(
            source_id=f"market_{scope}",
            title="Catalog lịch sử — thống kê tại một thời điểm theo bộ lọc đã chọn",
            kind=EvidenceKind.MARKET,
            entries=[("snapshot_product_count", product_count, "product")],
        )
        dimension_label = {
            "category": "thể loại",
            "author": "tác giả",
            "publisher": "nhà xuất bản",
            "price": "giá",
            "rating": "điểm đánh giá",
        }[request.dimension.value]
        for metric in sorted(metrics, key=lambda item: (-item.count, item.label))[
            :MARKET_EVIDENCE_BUCKET_LIMIT
        ]:
            bucket_id = _tool_id("bucket", {"label": metric.label})[-24:]
            builder.add(
                source_id=f"market_{scope}_{bucket_id}",
                title=(
                    f"Mục “{metric.label}” trong phân bố "
                    f"{dimension_label} của catalog lịch sử"
                ),
                kind=EvidenceKind.MARKET,
                entries=[
                    (f"market_{request.dimension.value}_count", metric.count, "product")
                ],
            )
        return MarketResult(
            snapshot_version_id=self.catalog_snapshot.version_id, metrics=metrics
        ), builder.build()

    async def _knowledge(
        self, request: KnowledgeRetrieveInput, access: ResourceAuthorization
    ) -> tuple[V2Contract, ToolEvidence]:
        service = self.knowledge_service
        snapshot = self.knowledge_snapshot
        if service is None or snapshot is None:
            raise ReadToolError("knowledge_unavailable")

        def check_catalog() -> None:
            with self.session_factory() as session:
                self._guard_snapshot(session)
                self._products(EcommerceRepository(session), request.product_ids)

        await asyncio.to_thread(check_catalog)
        response = await service.retrieve(
            request,
            access,
            corpus_version_id=snapshot.corpus_version_id,
            index_manifest_id=snapshot.index_manifest_id,
        )
        if response.result.corpus_version_id != snapshot.corpus_version_id:
            raise ReadToolError("knowledge_snapshot_mismatch")
        subjects: dict[str, list[str]] = {}
        for product_id in request.product_ids:
            authorized = await asyncio.to_thread(
                service.store.list_authorized_sources,
                snapshot,
                access,
                product_ids=(product_id,),
            )
            for source in authorized:
                subjects.setdefault(source.source_id, []).append(
                    f"product_{product_id}"
                )
        excerpts = tuple(
            EvidenceExcerpt(
                evidence_id=excerpt.evidence_id,
                source_id=excerpt.source_id,
                source_version_id=excerpt.source_version_id,
                chunk_id=excerpt.chunk_id,
                span_id=excerpt.span_id,
                subject_ids=tuple(subjects.get(excerpt.source_id, ())),
                exact_text=excerpt.excerpt,
            )
            for excerpt in response.result.excerpts
        )
        return response.result, ToolEvidence(
            excerpts=excerpts, references=response.evidence
        )

    async def reopen_knowledge(
        self, reference: EvidenceReference, access: ResourceAuthorization
    ) -> ResolvedKnowledgeEvidence:
        if self.knowledge_service is None or self.knowledge_snapshot is None:
            raise ReadToolError("knowledge_unavailable")
        return await self.knowledge_service.reopen_evidence(
            reference,
            access,
            corpus_version_id=self.knowledge_snapshot.corpus_version_id,
            index_manifest_id=self.knowledge_snapshot.index_manifest_id,
        )


class _EvidenceBuilder:
    """Bind each deterministic fact to its exact canonical tool record."""

    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot
        self.facts: list[StructuredFact] = []
        self.excerpts: list[EvidenceExcerpt] = []
        self.references: list[EvidenceReference] = []

    def add(
        self,
        *,
        source_id: str,
        title: str,
        kind: EvidenceKind,
        entries: list[FactEntry],
        subject_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> str:
        exact_text = "\n".join(
            f"{field}: {_fact_text(value)}{f' {unit}' if unit else ''}"
            for field, value, unit in entries
        )
        chunk_id = _tool_id(
            "tch",
            {
                "source": source_id,
                "version": self.snapshot.version_id,
                "text": exact_text,
            },
        )
        span_id = _tool_id(
            "tsp", {"chunk": chunk_id, "start": 0, "end": len(exact_text)}
        )
        evidence_id = stable_evidence_id(
            source_id=source_id,
            source_version_id=self.snapshot.version_id,
            chunk_id=chunk_id,
            span_id=span_id,
        )
        self.references.append(
            EvidenceReference(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=self.snapshot.version_id,
                chunk_id=chunk_id,
                span_id=span_id,
                display_label=f"[C{len(self.references) + 1}]",
                kind=kind,
                title=title,
                observed_at=observed_at or self.snapshot.observed_at,
            )
        )
        self.excerpts.append(
            EvidenceExcerpt(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=self.snapshot.version_id,
                chunk_id=chunk_id,
                span_id=span_id,
                subject_ids=(subject_id,) if subject_id else (),
                exact_text=exact_text,
            )
        )
        for field, value, unit in entries:
            self.facts.append(
                StructuredFact(
                    fact_id=_tool_id("fact", {"evidence": evidence_id, "field": field}),
                    subject_id=subject_id,
                    field=field,
                    value=value,
                    unit=unit,
                    data_version_id=self.snapshot.version_id,
                    evidence_ids=(evidence_id,),
                )
            )
        return evidence_id

    def build(self) -> ToolEvidence:
        return ToolEvidence(
            facts=tuple(self.facts),
            excerpts=tuple(self.excerpts),
            references=tuple(self.references),
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _product_observed_at(product: Product) -> datetime:
    if product.dataset_source is None:
        raise ReadToolError("catalog_snapshot_unavailable")
    return _aware(product.dataset_source.retrieved_at)


def _fact_text(value: str | int | Decimal | bool) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _tool_id(prefix: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(canonical).hexdigest()}"
