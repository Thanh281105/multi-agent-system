from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import TaskStatus
from app.db.base import Base
from app.knowledge.grounding import GroundingContractError, GroundingEvidenceError
from app.knowledge.v2_contracts import (
    ResolvedKnowledgeEvidence,
    sha256_utf8,
    stable_evidence_id,
)
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.shared.budget import (
    GENERATION_INPUT_TOKEN_LIMIT,
    BudgetCancelledError,
    BudgetLimitExceededError,
    ProviderBudgetContext,
    generation_payload_token_bound,
    provider_budget_scope,
)
from app.shared.model_runtime import ModelCallMetadata, ModelRuntimeError
from app.v2.answers import EvidenceRequirement, GroundedAnswerProducer
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import (
    ConversationMode,
    DialogueOutcome,
    EvidenceKind,
    EvidenceReference,
)
from app.v2.runtime_contracts import (
    EvidenceExcerpt,
    RuntimeOperation,
    StructuredFact,
    ToolEvidence,
    build_operation_key,
)
from app.v2.tools import V2ReadTools, load_catalog_snapshot

_OBSERVED_AT = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def answer_tool_sessions(tmp_path: Path) -> Any:
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'answer-tools.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        source = DatasetSource(
            id=91,
            dataset_id="answer-tool-fixture",
            dataset_version="2026-09-01",
            profile="test",
            source_url="https://example.test/answer-books",
            source_license="Synthetic test fixture",
            source_revision=1,
            raw_archive_sha256="1" * 64,
            snapshot_sha256="2" * 64,
            products_sha256="3" * 64,
            reviews_sha256="4" * 64,
            sampling_seed=1,
            product_count=5,
            review_count=0,
            retrieved_at=_OBSERVED_AT,
        )
        session.add(source)
        session.flush()
        session.add_all(
            [
                Product(
                    id=index,
                    source_id=source.id,
                    external_id=f"answer-book-{index}",
                    name=f"Sách kiểm thử câu trả lời {index}",
                    authors=[f"Tác giả {index}"],
                    publisher="Nhà xuất bản kiểm thử",
                    category="Công nghệ",
                    page_count=200 + index,
                    price=100_000 + index * 1_000,
                    rating=4.0,
                    sold_count=10,
                    source_review_count=0,
                    description="Synthetic answer fixture.",
                    platform="Tiki",
                )
                for index in range(1, 6)
            ]
        )
        session.commit()
    try:
        yield sessions
    finally:
        engine.dispose()


class QueueRuntime:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        value = output(kwargs) if callable(output) else output
        call_index = len(self.calls)
        return SimpleNamespace(
            value=value,
            metadata=ModelCallMetadata(
                call_id=f"mcall_{call_index:032x}",
                stage=kwargs["stage"],
                agent_id="grounding",
                model="model_snapshot",
                status="success",
                duration_ms=1,
                attempts=1,
            ),
        )


def _budget(scope_id: str) -> ProviderBudgetContext:
    return ProviderBudgetContext(
        ledger=SimpleNamespace(),
        scope_id=scope_id,
        purpose="chat",
    )


def _tool_access() -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="default",
            principal_id="answer-tool-user",
            mode=ConversationMode.SHOPPER,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read"}),
    )


def _tool_operation(
    tools: V2ReadTools,
    capability: str,
    parameters: dict[str, Any],
) -> RuntimeOperation:
    definition = tools.registry.capability(capability)
    canonical = definition.validate_input(parameters).model_dump(mode="json")
    versions = tools.data_version_ids(capability)
    return RuntimeOperation(
        step_id=f"step_{capability.replace('.', '_')}",
        capability=capability,
        service=definition.service,
        parameters=canonical,
        data_version_ids=versions,
        operation_key=build_operation_key(capability, canonical, versions),
    )


async def _mixed_tool_evidence(
    sessions: sessionmaker[Session],
) -> tuple[
    ToolEvidence,
    ResolvedKnowledgeEvidence,
    tuple[EvidenceRequirement, ...],
]:
    tools = V2ReadTools(sessions, catalog_snapshot=load_catalog_snapshot(sessions))
    product_ids = [1, 2, 3, 4, 5]
    results = [
        await tools.execute(
            _tool_operation(tools, capability, {"product_ids": product_ids}),
            _tool_access(),
        )
        for capability in ("product.compare", "review.retrieve", "trust.analyze")
    ]
    assert all(result.status == TaskStatus.SUCCESS for result in results)
    knowledge, resolved = _knowledge_evidence(marker="c", subject_id="product_1")
    merged = ToolEvidence(
        facts=tuple(fact for result in results for fact in result.evidence.facts),
        excerpts=tuple(
            excerpt for result in results for excerpt in result.evidence.excerpts
        )
        + knowledge.excerpts,
        references=tuple(
            reference for result in results for reference in result.evidence.references
        )
        + knowledge.references,
    )
    requirements = (
        *(
            EvidenceRequirement(
                EvidenceKind.CATALOG,
                subject_id=f"product_{product_id}",
                field="snapshot_price_vnd",
            )
            for product_id in product_ids
        ),
        *(
            EvidenceRequirement(
                kind,
                subject_id=f"product_{product_id}",
            )
            for kind in (EvidenceKind.REVIEW, EvidenceKind.TRUST)
            for product_id in product_ids
        ),
        EvidenceRequirement(EvidenceKind.KNOWLEDGE, subject_id="product_1"),
    )
    return merged, resolved, requirements


def _result_covers_requirement(
    result_evidence_ids: set[str],
    evidence: ToolEvidence,
    requirement: EvidenceRequirement,
) -> bool:
    references = {reference.evidence_id: reference for reference in evidence.references}
    excerpts = {excerpt.evidence_id: excerpt for excerpt in evidence.excerpts}
    for fact in evidence.facts:
        if requirement.field is not None and fact.field != requirement.field:
            continue
        if (
            requirement.subject_id is not None
            and fact.subject_id != requirement.subject_id
        ):
            continue
        if any(
            evidence_id in result_evidence_ids
            and references[evidence_id].kind == requirement.kind
            for evidence_id in fact.evidence_ids
        ):
            return True
    if requirement.field is not None:
        return False
    return any(
        evidence_id in result_evidence_ids
        and references[evidence_id].kind == requirement.kind
        and (
            requirement.subject_id is None
            or requirement.subject_id in excerpts[evidence_id].subject_ids
        )
        for evidence_id in excerpts
    )


def _model_catalog_covers_requirement(
    catalog: dict[str, Any],
    requirement: EvidenceRequirement,
) -> bool:
    if any(
        requirement.kind.value in group["k"]
        and (requirement.subject_id is None or group["s"] == requirement.subject_id)
        and (
            requirement.field is None
            or any(fact[1] == requirement.field for fact in group["f"])
        )
        for group in catalog["facts"]
    ):
        return True
    if requirement.field is not None:
        return False
    return any(
        extract["k"] == requirement.kind.value
        and (requirement.subject_id is None or requirement.subject_id in extract["s"])
        for extract in catalog["extracts"]
    )


def _model_error() -> ModelRuntimeError:
    metadata = ModelCallMetadata(
        call_id=f"mcall_{'f' * 32}",
        stage="answer.draft",
        agent_id="grounding",
        model="model_snapshot",
        status="failed",
        duration_ms=1,
        attempts=1,
        error_code="model_timeout",
    )
    return ModelRuntimeError("model_timeout", metadata)


def _catalog_fact(
    marker: str,
    *,
    subject_id: str,
    title: str,
    field: str,
    value: int,
    unit: str,
) -> tuple[StructuredFact, EvidenceExcerpt, EvidenceReference]:
    evidence_id = f"evd_catalog_{marker}"
    source_version_id = "catalog_v1"
    reference = EvidenceReference(
        evidence_id=evidence_id,
        source_id="src_catalog",
        source_version_id=source_version_id,
        chunk_id=f"chunk_{marker}",
        span_id=f"span_{marker}",
        display_label="[C1]",
        kind=EvidenceKind.CATALOG,
        title=title,
        observed_at=_OBSERVED_AT,
    )
    excerpt = EvidenceExcerpt(
        evidence_id=evidence_id,
        source_id=reference.source_id,
        source_version_id=reference.source_version_id,
        chunk_id=reference.chunk_id,
        span_id=reference.span_id,
        subject_ids=(subject_id,),
        exact_text=f"{title}: {field}={value} {unit}",
    )
    fact = StructuredFact(
        fact_id=f"fact_{marker}",
        subject_id=subject_id,
        field=field,
        value=value,
        unit=unit,
        data_version_id=source_version_id,
        evidence_ids=(evidence_id,),
    )
    return fact, excerpt, reference


def _knowledge_evidence(
    marker: str = "1",
    *,
    title: str = "Book Alpha",
    text: str = "Book Alpha contains a concise introduction to astronomy.",
    subject_id: str = "product_1",
) -> tuple[ToolEvidence, ResolvedKnowledgeEvidence]:
    source_id = f"src_book_{marker}"
    source_version_id = f"svr_{marker * 60}"
    chunk_id = f"chk_{marker * 60}"
    span_id = f"spn_{marker * 60}"
    evidence_id = stable_evidence_id(
        source_id=source_id,
        source_version_id=source_version_id,
        chunk_id=chunk_id,
        span_id=span_id,
    )
    reference = EvidenceReference(
        evidence_id=evidence_id,
        source_id=source_id,
        source_version_id=source_version_id,
        chunk_id=chunk_id,
        span_id=span_id,
        display_label="[C9]",
        kind=EvidenceKind.KNOWLEDGE,
        title=title,
        url=f"https://example.test/{marker}",
        observed_at=_OBSERVED_AT,
    )
    excerpt = EvidenceExcerpt(
        evidence_id=evidence_id,
        source_id=source_id,
        source_version_id=source_version_id,
        chunk_id=chunk_id,
        span_id=span_id,
        subject_ids=(subject_id,),
        exact_text=text,
    )
    resolved = ResolvedKnowledgeEvidence(
        evidence_id=evidence_id,
        corpus_version_id=f"cor_{'a' * 60}",
        source_id=source_id,
        source_version_id=source_version_id,
        chunk_id=chunk_id,
        span_id=span_id,
        title=title,
        url=f"https://example.test/{marker}",
        excerpt=text,
        content_hash=sha256_utf8(text),
        observed_at=_OBSERVED_AT,
    )
    return ToolEvidence(excerpts=(excerpt,), references=(reference,)), resolved


def _resolver(*items: ResolvedKnowledgeEvidence):
    by_id = {item.evidence_id: item for item in items}

    async def resolve(reference: EvidenceReference) -> ResolvedKnowledgeEvidence:
        return by_id[reference.evidence_id]

    return resolve


def _knowledge_draft(text: str, evidence_id: str, claim_id: str = "claim_model"):
    def build(kwargs: dict[str, Any]) -> Any:
        return kwargs["schema"](
            claims=[
                {
                    "claim_id": claim_id,
                    "text": text,
                    "evidence_ids": [evidence_id],
                }
            ]
        )

    return build


def _fact_draft(fact_id: str, claim_id: str = "claim_model_fact"):
    def build(kwargs: dict[str, Any]) -> Any:
        return kwargs["schema"](claims=[{"claim_id": claim_id, "fact_ids": [fact_id]}])

    return build


def _verdict(claim_id: str, entailed: bool):
    def build(kwargs: dict[str, Any]) -> Any:
        return kwargs["schema"](verdicts=[{"claim_id": claim_id, "entailed": entailed}])

    return build


@pytest.mark.asyncio
async def test_off_mode_makes_zero_generation_calls_and_relabels_citations() -> None:
    price, price_excerpt, price_ref = _catalog_fact(
        "alpha_price",
        subject_id="product_1",
        title="Book Alpha — historical snapshot",
        field="snapshot_price_vnd",
        value=100,
        unit="VND",
    )
    pages, pages_excerpt, pages_ref = _catalog_fact(
        "beta_pages",
        subject_id="product_2",
        title="Book Beta — historical snapshot",
        field="page_count",
        value=100,
        unit="page",
    )
    runtime = QueueRuntime([])
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="off", model="model_snapshot"
    )

    result = await producer.produce(
        user_request="Compare the checked values.",
        evidence=ToolEvidence(
            facts=(price, pages),
            excerpts=(price_excerpt, pages_excerpt),
            references=(price_ref, pages_ref),
        ),
        allowed_subject_ids=frozenset({"product_1", "product_2"}),
    )

    assert runtime.calls == []
    assert result.outcome == DialogueOutcome.ANSWERED
    assert "Giá trong snapshot: 100 VND" in result.answer
    assert "Số trang: 100 trang" in result.answer
    assert [item.display_label for item in result.evidence] == ["[C1]", "[C2]"]
    assert {item.evidence_id for item in result.evidence} == {
        price_ref.evidence_id,
        pages_ref.evidence_id,
    }


@pytest.mark.asyncio
async def test_model_fact_selector_cannot_move_value_to_another_entity_or_field() -> (
    None
):
    pages, excerpt, reference = _catalog_fact(
        "beta_pages",
        subject_id="product_2",
        title="Book Beta — historical snapshot",
        field="page_count",
        value=100,
        unit="page",
    )
    runtime = QueueRuntime([_fact_draft(pages.fact_id)])
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_fact_scope")):
        result = await producer.produce(
            user_request="What does the selected fact say?",
            evidence=ToolEvidence(
                facts=(pages,), excerpts=(excerpt,), references=(reference,)
            ),
            allowed_subject_ids=frozenset({"product_2"}),
        )

    assert len(runtime.calls) == 1
    assert "Book Beta" in result.answer
    assert "Số trang: 100 trang" in result.answer
    assert "Book Alpha" not in result.answer
    assert "Giá" not in result.answer


@pytest.mark.asyncio
async def test_global_market_fact_uses_reference_context_without_fake_subject() -> None:
    reference = EvidenceReference(
        evidence_id="evd_market_business",
        source_id="src_market",
        source_version_id="market_v1",
        chunk_id="chunk_market_business",
        span_id="span_market_business",
        display_label="[C4]",
        kind=EvidenceKind.MARKET,
        title="Thể loại “Kinh doanh” trong snapshot",
        observed_at=_OBSERVED_AT,
    )
    excerpt = EvidenceExcerpt(
        evidence_id=reference.evidence_id,
        source_id=reference.source_id,
        source_version_id=reference.source_version_id,
        chunk_id=reference.chunk_id,
        span_id=reference.span_id,
        subject_ids=(),
        exact_text="Snapshot có 12 sách trong bucket Kinh doanh.",
    )
    fact = StructuredFact(
        fact_id="fact_market_business",
        subject_id=None,
        field="market_category_count",
        value=12,
        unit="product",
        data_version_id="market_v1",
        evidence_ids=(reference.evidence_id,),
    )
    producer = GroundedAnswerProducer(None, runtime_mode="off", model=None)

    result = await producer.produce(
        user_request="How many books are in this returned bucket?",
        evidence=ToolEvidence(
            facts=(fact,), excerpts=(excerpt,), references=(reference,)
        ),
        allowed_subject_ids=frozenset(),
    )

    assert "Thể loại “Kinh doanh” trong snapshot" in result.answer
    assert "Số sản phẩm: 12 sản phẩm" in result.answer
    assert "product_" not in result.answer


@pytest.mark.asyncio
async def test_hybrid_publishes_only_semantically_checked_model_claim() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    claim_text = "Book Alpha offers a short introduction to astronomy."
    runtime = QueueRuntime(
        [
            _knowledge_draft(claim_text, evidence_id),
            _verdict("claim_model", True),
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_hybrid_scope")):
        result = await producer.produce(
            user_request="What does Book Alpha cover?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
        )

    assert result.answer == f"{claim_text} [C1]"
    assert result.claims[0].text == claim_text
    assert result.citations[0].evidence_id == evidence_id
    assert [call["stage"] for call in runtime.calls] == [
        "answer.draft",
        "answer.grounding",
    ]


@pytest.mark.asyncio
async def test_one_repair_is_verified_before_publication() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    initial = "Book Alpha is ideal for every astronomy student."
    repaired = "Book Alpha contains an introduction to astronomy."
    runtime = QueueRuntime(
        [
            _knowledge_draft(initial, evidence_id),
            _verdict("claim_model", False),
            _knowledge_draft(repaired, evidence_id, "claim_repaired"),
            _verdict("claim_repaired", True),
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_repair_scope")):
        result = await producer.produce(
            user_request="What does Book Alpha cover?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
            allow_repair=True,
        )

    assert result.draft_repairs == 1
    assert result.claims[0].text == repaired
    assert "answer_draft_repaired" in result.warnings
    assert [call["stage"] for call in runtime.calls] == [
        "answer.draft",
        "answer.grounding",
        "answer.repair",
        "answer.grounding",
    ]


@pytest.mark.asyncio
async def test_repair_is_never_attempted_without_explicit_caller_allowance() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    runtime = QueueRuntime(
        [
            _knowledge_draft(
                "Book Alpha is ideal for every astronomy student.", evidence_id
            ),
            _verdict("claim_model", False),
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_no_repair_scope")):
        result = await producer.produce(
            user_request="What does Book Alpha cover?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
        )

    assert len(runtime.calls) == 2
    assert result.draft_repairs == 0
    assert result.claims[0].text == evidence.excerpts[0].exact_text
    assert result.answer.startswith("“")
    assert "answer_draft_not_grounded" in result.warnings


@pytest.mark.asyncio
async def test_repair_reopens_all_knowledge_before_authorization_and_dispatch() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    revoked = False
    resolver_calls = 0
    repair_checks = 0

    async def resolve(reference: EvidenceReference) -> ResolvedKnowledgeEvidence:
        nonlocal resolver_calls
        resolver_calls += 1
        if revoked:
            raise GroundingEvidenceError("knowledge_access_revoked")
        assert reference.evidence_id == resolved.evidence_id
        return resolved

    def reject_and_revoke(kwargs: dict[str, Any]) -> Any:
        nonlocal revoked
        revoked = True
        return kwargs["schema"](
            verdicts=[{"claim_id": "claim_model", "entailed": False}]
        )

    def authorize_repair() -> bool:
        nonlocal repair_checks
        repair_checks += 1
        return True

    runtime = QueueRuntime(
        [
            _knowledge_draft("Book Alpha is ideal for every reader.", evidence_id),
            reject_and_revoke,
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_repair_revocation_scope")):
        with pytest.raises(GroundingEvidenceError, match="knowledge_access_revoked"):
            await producer.produce(
                user_request="Who should read Book Alpha?",
                evidence=evidence,
                allowed_subject_ids=frozenset({"product_1"}),
                resolve_knowledge=resolve,
                allow_repair=authorize_repair,
            )

    assert resolver_calls == 3
    assert repair_checks == 0
    assert [call["stage"] for call in runtime.calls] == [
        "answer.draft",
        "answer.grounding",
    ]


@pytest.mark.asyncio
async def test_failed_repair_stops_after_one_and_returns_exact_extract() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    runtime = QueueRuntime(
        [
            _knowledge_draft("Book Alpha is best for everyone.", evidence_id),
            _verdict("claim_model", False),
            _knowledge_draft(
                "Book Alpha is perfect for all readers.",
                evidence_id,
                "claim_repaired",
            ),
            _verdict("claim_repaired", False),
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_failed_repair_scope")):
        result = await producer.produce(
            user_request="Who should read Book Alpha?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
            allow_repair=True,
        )

    assert len(runtime.calls) == 4
    assert result.draft_repairs == 1
    assert result.claims[0].text == evidence.excerpts[0].exact_text
    assert "answer_repair_not_grounded" in result.warnings


@pytest.mark.asyncio
async def test_shadow_records_model_path_but_publishes_deterministic_extract() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    model_text = "Book Alpha provides an introductory astronomy overview."
    runtime = QueueRuntime(
        [
            _knowledge_draft(model_text, evidence_id),
            _verdict("claim_model", True),
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="shadow", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_shadow_scope")):
        result = await producer.produce(
            user_request="What does Book Alpha cover?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
        )

    assert len(runtime.calls) == 2
    assert result.claims[0].text == evidence.excerpts[0].exact_text
    assert model_text not in result.answer
    assert "shadow_model_choice_not_published" in result.warnings


@pytest.mark.asyncio
async def test_hybrid_model_error_falls_back_to_exact_checked_extract() -> None:
    evidence, resolved = _knowledge_evidence()
    runtime = QueueRuntime([_model_error()])
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_hybrid_error_scope")):
        result = await producer.produce(
            user_request="What does Book Alpha cover?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
        )

    assert result.claims[0].text == evidence.excerpts[0].exact_text
    assert "answer_model_runtime_error" in result.warnings


@pytest.mark.asyncio
async def test_required_model_error_is_never_deterministic_success() -> None:
    evidence, resolved = _knowledge_evidence()
    runtime = QueueRuntime([_model_error()])
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="required", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_required_error_scope")):
        with pytest.raises(ModelRuntimeError, match="model_timeout"):
            await producer.produce(
                user_request="What does Book Alpha cover?",
                evidence=evidence,
                allowed_subject_ids=frozenset({"product_1"}),
                resolve_knowledge=_resolver(resolved),
            )


@pytest.mark.asyncio
async def test_required_unknown_verifier_output_fails_closed() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    runtime = QueueRuntime(
        [
            _knowledge_draft(
                "Book Alpha provides an astronomy introduction.", evidence_id
            ),
            _verdict("claim_unknown", True),
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="required", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_required_contract_scope")):
        with pytest.raises(
            GroundingContractError, match="semantic_verdict_claim_set_mismatch"
        ):
            await producer.produce(
                user_request="What does Book Alpha cover?",
                evidence=evidence,
                allowed_subject_ids=frozenset({"product_1"}),
                resolve_knowledge=_resolver(resolved),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        BudgetLimitExceededError("budget_limit"),
        BudgetCancelledError("provider_dispatch_cancelled"),
        asyncio.CancelledError(),
    ),
)
async def test_budget_and_cancellation_errors_propagate(error: BaseException) -> None:
    evidence, resolved = _knowledge_evidence()
    runtime = QueueRuntime([error])
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_budget_error_scope")):
        with pytest.raises(type(error)):
            await producer.produce(
                user_request="What does Book Alpha cover?",
                evidence=evidence,
                allowed_subject_ids=frozenset({"product_1"}),
                resolve_knowledge=_resolver(resolved),
            )


@pytest.mark.asyncio
async def test_free_model_answer_field_cannot_bypass_claim_verification() -> None:
    evidence, resolved = _knowledge_evidence()
    runtime = QueueRuntime(
        [
            {
                "answer": "Book Alpha costs 999999 VND.",
                "claims": [],
            }
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_free_text_scope")):
        result = await producer.produce(
            user_request="What does Book Alpha cover?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
        )

    assert "999999" not in result.answer
    assert result.claims[0].text == evidence.excerpts[0].exact_text
    assert "answer_model_contract_error" in result.warnings


@pytest.mark.asyncio
async def test_model_context_is_rejected_before_dispatch_for_a_foreign_subject() -> (
    None
):
    evidence, resolved = _knowledge_evidence(subject_id="product_2")
    runtime = QueueRuntime([])
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_foreign_context_scope")):
        with pytest.raises(
            GroundingEvidenceError, match="evidence_subject_outside_request"
        ):
            await producer.produce(
                user_request="What does this source say?",
                evidence=evidence,
                allowed_subject_ids=frozenset({"product_1"}),
                resolve_knowledge=_resolver(resolved),
            )

    assert runtime.calls == []


@pytest.mark.asyncio
async def test_wrong_entity_field_number_does_not_satisfy_required_price() -> None:
    price, price_excerpt, price_ref = _catalog_fact(
        "alpha_required_price",
        subject_id="product_1",
        title="Book Alpha — historical snapshot",
        field="snapshot_price_vnd",
        value=100,
        unit="VND",
    )
    pages, pages_excerpt, pages_ref = _catalog_fact(
        "beta_selected_pages",
        subject_id="product_2",
        title="Book Beta — historical snapshot",
        field="page_count",
        value=100,
        unit="page",
    )
    runtime = QueueRuntime([_fact_draft(pages.fact_id)])
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )

    with provider_budget_scope(_budget("answer_required_price_scope")):
        result = await producer.produce(
            user_request="What is Book Alpha's checked price?",
            evidence=ToolEvidence(
                facts=(price, pages),
                excerpts=(price_excerpt, pages_excerpt),
                references=(price_ref, pages_ref),
            ),
            allowed_subject_ids=frozenset({"product_1", "product_2"}),
            requirements=(
                EvidenceRequirement(
                    EvidenceKind.CATALOG,
                    subject_id="product_1",
                    field="snapshot_price_vnd",
                ),
            ),
        )

    assert len(runtime.calls) == 1
    assert "Giá trong snapshot: 100 VND" in result.answer
    assert "answer_draft_missing_required_evidence" in result.warnings


@pytest.mark.asyncio
async def test_repair_authorization_is_rechecked_once_at_the_decision_point() -> None:
    evidence, resolved = _knowledge_evidence()
    evidence_id = evidence.references[0].evidence_id
    runtime = QueueRuntime(
        [
            _knowledge_draft("Book Alpha is best for every reader.", evidence_id),
            _verdict("claim_model", False),
        ]
    )
    producer = GroundedAnswerProducer(
        runtime, runtime_mode="hybrid", model="model_snapshot"
    )
    checks = 0

    def repair_still_available() -> bool:
        nonlocal checks
        checks += 1
        return False

    with provider_budget_scope(_budget("answer_fresh_repair_scope")):
        result = await producer.produce(
            user_request="Who should read Book Alpha?",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            resolve_knowledge=_resolver(resolved),
            allow_repair=repair_still_available,
        )

    assert checks == 1
    assert len(runtime.calls) == 2
    assert result.draft_repairs == 0
    assert result.claims[0].text == evidence.excerpts[0].exact_text


@pytest.mark.asyncio
async def test_long_fact_group_splits_without_losing_a_required_field() -> None:
    reference = EvidenceReference(
        evidence_id="evd_long_fact_group",
        source_id="src_long_fact_group",
        source_version_id="catalog_long_v1",
        chunk_id="chunk_long_fact_group",
        span_id="span_long_fact_group",
        display_label="[C1]",
        kind=EvidenceKind.CATALOG,
        title=f"{'S' * 270} — snapshot",
        observed_at=_OBSERVED_AT,
    )
    excerpt = EvidenceExcerpt(
        evidence_id=reference.evidence_id,
        source_id=reference.source_id,
        source_version_id=reference.source_version_id,
        chunk_id=reference.chunk_id,
        span_id=reference.span_id,
        subject_ids=("product_1",),
        exact_text="Server-authored long catalog record.",
    )
    facts = tuple(
        StructuredFact(
            fact_id=f"fact_long_{field}",
            subject_id="product_1",
            field=field,
            value=value,
            unit=unit,
            data_version_id=reference.source_version_id,
            evidence_ids=(reference.evidence_id,),
        )
        for field, value, unit in (
            ("title", "T" * 300, None),
            ("author", "A" * 250, None),
            ("publisher", "P" * 250, None),
            ("snapshot_price_vnd", 123_000, "VND"),
        )
    )

    result = await GroundedAnswerProducer(None, runtime_mode="off", model=None).produce(
        user_request="Show the checked long record and its price.",
        evidence=ToolEvidence(
            facts=facts,
            excerpts=(excerpt,),
            references=(reference,),
        ),
        allowed_subject_ids=frozenset({"product_1"}),
        requirements=(
            EvidenceRequirement(
                EvidenceKind.CATALOG,
                subject_id="product_1",
                field="snapshot_price_vnd",
            ),
        ),
    )

    assert len(result.claims) > 1
    assert all(len(claim.text) <= 1_000 for claim in result.claims)
    assert "Giá trong snapshot: 123000 VND" in result.answer
    assert "Nhà xuất bản" in result.answer


@pytest.mark.asyncio
async def test_real_read_tools_keep_all_explicit_kinds_subjects_and_price_fields(
    answer_tool_sessions: sessionmaker[Session],
) -> None:
    evidence, resolved, requirements = await _mixed_tool_evidence(answer_tool_sessions)
    assert len(evidence.facts) > 20

    result = await GroundedAnswerProducer(None, runtime_mode="off", model=None).produce(
        user_request="Compare prices, reviews, trust limits, and the source.",
        evidence=evidence,
        allowed_subject_ids=frozenset(
            {f"product_{product_id}" for product_id in range(1, 6)}
        ),
        resolve_knowledge=_resolver(resolved),
        requirements=requirements,
    )

    emitted_ids = {reference.evidence_id for reference in result.evidence}
    assert result.outcome == DialogueOutcome.ANSWERED
    assert len(result.claims) <= 20
    assert all(
        _result_covers_requirement(emitted_ids, evidence, requirement)
        for requirement in requirements
    )
    assert {reference.kind for reference in result.evidence} == {
        EvidenceKind.CATALOG,
        EvidenceKind.REVIEW,
        EvidenceKind.TRUST,
        EvidenceKind.KNOWLEDGE,
    }


@pytest.mark.asyncio
async def test_real_tool_draft_repair_and_verifier_payloads_fit_shared_bound(
    answer_tool_sessions: sessionmaker[Session],
) -> None:
    from openai.lib._parsing._responses import type_to_text_format_param

    evidence, resolved, requirements = await _mixed_tool_evidence(answer_tool_sessions)
    evidence_id = next(
        reference.evidence_id
        for reference in evidence.references
        if reference.kind == EvidenceKind.KNOWLEDGE
    )
    runtime = QueueRuntime(
        [
            _knowledge_draft("Book Alpha is ideal for every reader.", evidence_id),
            _verdict("claim_model", False),
            _knowledge_draft(
                "Book Alpha contains a concise astronomy introduction.",
                evidence_id,
                "claim_repaired",
            ),
            _verdict("claim_repaired", True),
        ]
    )
    repair_checks = 0

    def repair_still_available() -> bool:
        nonlocal repair_checks
        repair_checks += 1
        return True

    with provider_budget_scope(_budget("answer_real_tool_payload_scope")):
        result = await GroundedAnswerProducer(
            runtime, runtime_mode="hybrid", model="model_snapshot"
        ).produce(
            user_request="Compare prices, reviews, trust limits, and the source.",
            evidence=evidence,
            allowed_subject_ids=frozenset(
                {f"product_{product_id}" for product_id in range(1, 6)}
            ),
            resolve_knowledge=_resolver(resolved),
            allow_repair=repair_still_available,
            requirements=requirements,
        )

    assert repair_checks == 1
    assert result.draft_repairs == 1
    assert [call["stage"] for call in runtime.calls] == [
        "answer.draft",
        "answer.grounding",
        "answer.repair",
        "answer.grounding",
    ]
    for call in runtime.calls:
        bound = generation_payload_token_bound(
            instructions=call["instructions"],
            input_text=call["input_text"],
            text_format=type_to_text_format_param(call["schema"]),
        )
        assert bound <= GENERATION_INPUT_TOKEN_LIMIT

    draft_input = json.loads(runtime.calls[0]["input_text"])
    repair_input = json.loads(runtime.calls[2]["input_text"])
    expected_requirements = [
        [requirement.kind.value, requirement.subject_id, requirement.field]
        for requirement in requirements
    ]
    assert draft_input["required"] == expected_requirements
    assert repair_input["required"] == expected_requirements
    assert all(
        _model_catalog_covers_requirement(draft_input["catalog"], requirement)
        for requirement in requirements
    )
    assert all(
        _model_catalog_covers_requirement(repair_input["catalog"], requirement)
        for requirement in requirements
    )
    assert all(
        extract["k"] == EvidenceKind.KNOWLEDGE.value
        for extract in draft_input["catalog"]["extracts"]
    )
    assert "source_version_id" not in runtime.calls[0]["input_text"]
    assert "data_version_id" not in runtime.calls[0]["input_text"]
