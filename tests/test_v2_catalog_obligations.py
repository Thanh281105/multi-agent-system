"""Catalog coverage must stop synthesis when a requested subject is missing."""

from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic

import pytest

from app.contracts import TaskStatus
from app.v2.answers import GroundedAnswerProducer
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import ConversationMode, DialogueOutcome, EvidenceKind
from app.v2.execution import OperationBatch
from app.v2.planning import BoundedV2Planner, PlanningContext, RuntimeDataVersions
from app.v2.registry import ProductCandidate, ProductResult
from app.v2.runtime_contracts import (
    EvidenceObligation,
    EvidenceObligationKind,
    ExpertResult,
)
from app.v2.supervisor import V2ReadSupervisor, _BudgetRemaining, assess_evidence
from app.v2.tools import CatalogSnapshot, _EvidenceBuilder


def _no_database():
    raise AssertionError("catalog coverage tests must not open a database")


def _context(product_ids):
    return PlanningContext(
        access=ResourceAuthorization(
            binding=ResourceBinding(
                tenant_id="tenant_test",
                principal_id="shopper_test",
                mode=ConversationMode.SHOPPER,
                store_id="demo",
            ),
            scopes=frozenset({"ecommerce.read"}),
        ),
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_test",
            corpus_version_id="corpus_test",
            index_manifest_id="index_test",
        ),
        resolved_product_ids=product_ids,
    )


class CatalogExecutor:
    def __init__(self, product_ids, evidence_product_ids, evidence_kind):
        self.product_ids = product_ids
        self.evidence_product_ids = evidence_product_ids
        self.evidence_kind = evidence_kind
        self.calls = 0

    async def execute(self, *, operations, **kwargs):
        self.calls += 1
        assert len(operations) == 1
        operation = operations[0]
        assert operation.capability == "product.catalog.search"
        now = datetime.now(UTC)
        builder = _EvidenceBuilder(CatalogSnapshot("catalog_test", now, (1,)))
        for product_id in self.evidence_product_ids:
            builder.add(
                source_id=f"catalog_product_{product_id}",
                subject_id=f"product_{product_id}",
                title=f"Book {product_id} catalog",
                kind=self.evidence_kind,
                entries=[
                    ("title", f"Book {product_id}", None),
                    ("snapshot_price_vnd", 100_000 + product_id, "VND"),
                ],
            )
        evidence = builder.build()
        output = ProductResult(
            products=tuple(
                ProductCandidate(
                    product_id=product_id,
                    title=f"Book {product_id}",
                    price_vnd=100_000 + product_id,
                    catalog_version_id="catalog_test",
                )
                for product_id in self.product_ids
            ),
            evidence=evidence.references,
        )
        result = ExpertResult(
            operation=operation,
            status=TaskStatus.SUCCESS,
            output=output.model_dump(mode="json"),
            evidence=evidence,
            started_at=now,
            completed_at=now,
        )
        return OperationBatch(
            results=(result,),
            dispatched_step_ids=(operation.step_id,),
            reused_step_ids=(),
        )


class CountingAnswerProducer:
    def __init__(self):
        self.calls = 0
        self.native = GroundedAnswerProducer(None, runtime_mode="off", model=None)

    async def produce(self, **kwargs):
        self.calls += 1
        return await self.native.produce(**kwargs)


async def _run(expected_ids, actual_ids, evidence_ids, *, kind, continuation=True):
    executor = CatalogExecutor(actual_ids, evidence_ids, kind)
    producer = CountingAnswerProducer()
    supervisor = V2ReadSupervisor(
        _no_database,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,
        answer_producer=producer,
        continuation_enabled=continuation,
    )
    computation = await supervisor.run_claimed(
        conversation_id="conversation_test",
        turn_id="turn_test",
        lease_owner="lease_test",
        message="Tìm sách kinh tế",
        context=_context(expected_ids),
        deadline_monotonic=monotonic() + 60,
    )
    return computation.result, executor, producer


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize(
    ("actual_ids", "evidence_ids", "kind"),
    [
        ((7,), (7,), EvidenceKind.CATALOG),
        ((7, 8), (7,), EvidenceKind.CATALOG),
        ((7, 8), (7, 8), EvidenceKind.SANDBOX),
    ],
)
async def test_incomplete_named_catalog_stops_before_synthesis(
    actual_ids, evidence_ids, kind, continuation
):
    result, executor, producer = await _run(
        (7, 8), actual_ids, evidence_ids, kind=kind, continuation=continuation
    )
    assert result.outcome == DialogueOutcome.ABSTAINED
    assert "evidence_incomplete" in result.warnings
    assert result.claims == result.citations == ()
    assert executor.calls == 1
    assert producer.calls == 0


@pytest.mark.asyncio
async def test_complete_named_catalog_keeps_native_grounding_and_citations():
    result, executor, producer = await _run(
        (7, 8), (7, 8), (7, 8), kind=EvidenceKind.CATALOG
    )
    assert result.outcome == DialogueOutcome.ANSWERED
    assert executor.calls == producer.calls == 1
    assert "Book 7" in result.answer and "Book 8" in result.answer
    assert {reference.source_id for reference in result.evidence} == {
        "catalog_product_7",
        "catalog_product_8",
    }
    assert {citation.evidence_id for citation in result.citations} == {
        reference.evidence_id for reference in result.evidence
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_ids", [(), (7,)])
async def test_generic_catalog_keeps_empty_and_found_candidate_behavior(actual_ids):
    result, _, producer = await _run(
        (), actual_ids, actual_ids, kind=EvidenceKind.CATALOG
    )
    if actual_ids:
        assert result.outcome == DialogueOutcome.ANSWERED
        assert producer.calls == 1
    else:
        assert result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
        assert "evidence_incomplete" in result.warnings
        assert producer.calls == 0


def test_missing_named_catalog_stays_missing_even_with_unrelated_candidate_ids():
    obligation = EvidenceObligation(
        obligation_id="obl_catalog",
        kind=EvidenceObligationKind.CATALOG,
        description="catalog candidates",
        explicit=True,
        product_ids=(7, 8),
    )
    from app.v2.supervisor import _obligation_satisfied

    class OtherRead:
        status = TaskStatus.SUCCESS
        operation = type("Operation", (), {"capability": "review.retrieve"})()

    assert not _obligation_satisfied(obligation, (OtherRead(),), (7, 8))


def test_empty_catalog_assessment_preserves_missing_obligation():
    obligation = EvidenceObligation(
        obligation_id="obl_catalog",
        kind=EvidenceObligationKind.CATALOG,
        description="catalog candidates",
        explicit=True,
        product_ids=(7, 8),
    )
    assessment = assess_evidence(
        (obligation,),
        (),
        remaining=_BudgetRemaining(60.0, 10, 16, Decimal("0.25")),
        plan_revisions_used=0,
        added_reads_used=0,
        draft_repairs_used=0,
    )
    assert assessment.missing_obligation_ids == ("obl_catalog",)
    assert assessment.fulfilled_obligation_ids == ()
