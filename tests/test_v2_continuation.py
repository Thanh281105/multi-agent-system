"""Continuation invariants for bounded v2 read planning."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts import AuthorizationContext
from app.v2.authorization import bind_request_authorization
from app.v2.contracts import ConversationMode
from app.v2.planning import (
    BoundedV2Planner,
    PlanningContext,
    RuntimeDataVersions,
)
from app.v2.runtime_contracts import EvidenceAssessment, RuntimeOperation


@pytest.mark.asyncio
async def test_initial_candidate_binding_compiles_full_declared_dependency_set() -> (
    None
):
    planner = BoundedV2Planner(runtime_mode="off")
    context = _context()
    planned = await planner.plan(
        "Tìm sách 'Sapiens', so sánh, cho biết chủ đề, review và phàn nàn",
        context,
    )
    assert [item.capability for item in planned.initial_operations] == [
        "product.catalog.search"
    ]
    bound = planner.bind_initial_candidates(
        planned,
        (1, 2),
        context,
        planned.initial_operations,
    )
    assert [item.capability for item in bound] == [
        "product.compare",
        "review.compare",
        "trust.compare",
        "knowledge.retrieve",
    ]
    assert all(item.depends_on == ("step_001",) for item in bound)
    assert all(item.parameters.get("product_ids") == [1, 2] for item in bound)


@pytest.mark.asyncio
async def test_evidence_continuation_returns_only_two_new_operation_keys() -> None:
    planner = BoundedV2Planner(runtime_mode="off")
    context = _context()
    planned = await planner.plan(
        "Tìm sách 'Sapiens', so sánh, cho biết chủ đề, review và phàn nàn",
        context,
    )
    assessment = _assessment(
        tuple(item.obligation_id for item in planned.obligations),
        candidates=(1, 2),
        expert_steps=1,
    )
    added = planner.continue_plan(
        planned,
        assessment,
        context,
        planned.initial_operations,
    )
    assert len(added) == 2
    assert not {item.operation_key for item in added} & {
        item.operation_key for item in planned.initial_operations
    }
    assert len(planned.initial_operations) + len(added) <= 8


@pytest.mark.asyncio
async def test_knowledge_continuation_is_one_distinct_expansion_then_stops() -> None:
    planner = BoundedV2Planner(runtime_mode="off")
    context = _context()
    planned = await planner.plan("Sách này nói về chủ đề gì?", context)
    first = planned.initial_operations[0]
    obligation_id = planned.obligations[0].obligation_id
    second_batch = planner.continue_plan(
        planned,
        _assessment((obligation_id,), expert_steps=1, knowledge=1),
        context,
        (first,),
    )
    assert len(second_batch) == 1
    second = second_batch[0]
    assert second.capability == "knowledge.retrieve"
    assert second.parameters["top_k"] == 8
    assert second.operation_key != first.operation_key
    stopped = planner.continue_plan(
        planned,
        _assessment(
            (obligation_id,),
            expert_steps=2,
            knowledge=2,
            plan_revisions=1,
            added_reads=1,
        ),
        context,
        (first, second),
    )
    assert stopped == ()


@pytest.mark.asyncio
async def test_operation_key_rejects_post_plan_parameter_mutation() -> None:
    operation = (
        await BoundedV2Planner(runtime_mode="off").plan("Tìm sách Sapiens", _context())
    ).initial_operations[0]
    operation.parameters["candidate_limit"] = 4
    with pytest.raises(ValidationError, match="operation key"):
        RuntimeOperation.model_validate(operation.model_dump(mode="python"))


@pytest.mark.asyncio
async def test_data_version_change_produces_a_distinct_operation_key() -> None:
    first = (
        await BoundedV2Planner(runtime_mode="off").plan(
            "Tìm sách Sapiens", _context(catalog_version="catalog_v1")
        )
    ).initial_operations[0]
    second = (
        await BoundedV2Planner(runtime_mode="off").plan(
            "Tìm sách Sapiens", _context(catalog_version="catalog_v2")
        )
    ).initial_operations[0]
    assert first.parameters == second.parameters
    assert first.operation_key != second.operation_key


def _context(*, catalog_version: str = "catalog_v1") -> PlanningContext:
    authorization = AuthorizationContext(
        principal_id="principal_continuation",
        tenant_id="tenant_continuation",
        scopes=frozenset({"ecommerce.read"}),
    )
    return PlanningContext(
        access=bind_request_authorization(authorization, ConversationMode.SHOPPER),
        versions=RuntimeDataVersions(
            catalog_version_id=catalog_version,
            corpus_version_id="corpus_v1",
            index_manifest_id="index_v1",
        ),
    )


def _assessment(
    missing: tuple[str, ...],
    *,
    candidates: tuple[int, ...] = (),
    expert_steps: int = 0,
    knowledge: int = 0,
    plan_revisions: int = 0,
    added_reads: int = 0,
) -> EvidenceAssessment:
    return EvidenceAssessment(
        missing_obligation_ids=missing,
        candidate_product_ids=candidates,
        remaining_seconds=60,
        remaining_generation_calls=10,
        remaining_provider_attempts=16,
        remaining_cost_usd=Decimal("0.25"),
        plan_revisions_used=plan_revisions,
        expert_steps_used=expert_steps,
        added_reads_used=added_reads,
        knowledge_retrievals_used=knowledge,
        draft_repairs_used=0,
    )
