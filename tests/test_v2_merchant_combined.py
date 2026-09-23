"""Ground both inventory prices before proposing the server-selected offer."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.contracts import TaskStatus
from app.knowledge.grounding import GroundingEvidenceError, GroundingVerifier
from app.shared.budget import provider_budget_scope
from app.v2.answers import GroundedAnswerProducer
from app.v2.contracts import ConversationMode, DialogueOutcome, SafeExecutionError
from app.v2.execution import OperationBatch
from app.v2.planning import (
    BoundedV2Planner,
    ModelPlanRejectedError,
    PlanningContext,
    ResolvedMerchantTarget,
)
from app.v2.registry import MerchantOffer, MerchantReadResult
from app.v2.runtime_contracts import ExpertResult
from app.v2.supervisor import V2ReadSupervisor
from app.v2.tools import CatalogSnapshot, V2ReadTools
from tests.test_v2_supervisor import (
    _budget_context,
    _ChoiceRuntime,
    _context,
    _FakeActionService,
    _unused_session_factory,
)

MESSAGE = "Kiểm tra giá hai sản phẩm, rồi đổi giá sản phẩm đã chọn thành 70.900 đồng."


def _merchant_context(product_ids=(8, 7), **target_changes):
    context = _context(
        mode=ConversationMode.MERCHANT,
        resolved_product_ids=product_ids,
        write=True,
    )
    target = ResolvedMerchantTarget(
        **{
            "product_id": 7,
            "offer_id": "offer_server_owned",
            "expected_version": 6,
            **target_changes,
        }
    )
    return PlanningContext.model_validate(
        {
            **context.model_dump(mode="python"),
            "merchant_target": target,
        }
    )


class InventoryExecutor:
    def __init__(self, *, reverse=False, missing=False, failed=False, no_price=False):
        offers = [
            MerchantOffer(
                offer_id="offer_other",
                product_id=8,
                price_vnd=72000,
                available_quantity=7,
                version=1,
            ),
            MerchantOffer(
                offer_id="offer_server_owned",
                product_id=7,
                price_vnd=75900,
                available_quantity=12,
                version=6,
            ),
        ]
        if reverse:
            offers.reverse()
        if missing:
            offers = offers[1:]
        self.inventory = MerchantReadResult(
            offers=tuple(offers),
            snapshot_version_id="sandbox_snapshot_test",
        )
        self.failed = failed
        self.no_price = no_price
        self.calls = 0

    async def execute(self, *, operations, **kwargs):
        self.calls += 1
        assert len(operations) == 1
        operation = operations[0]
        assert operation.capability == "merchant.inventory.read"
        assert set(operation.parameters["product_ids"]) == {7, 8}
        now = datetime.now(UTC)
        if self.failed:
            result = ExpertResult(
                operation=operation,
                status=TaskStatus.FAILED,
                error=SafeExecutionError(
                    code="inventory_unavailable", message="Inventory unavailable."
                ),
                started_at=now,
                completed_at=now,
            )
        else:
            tools = V2ReadTools(
                _unused_session_factory,
                catalog_snapshot=CatalogSnapshot("catalog_test", now, (1,)),
            )
            evidence = tools._sandbox_action_evidence(
                "merchant.inventory.read",
                self.inventory,
            )
            if self.no_price:
                evidence = evidence.model_copy(
                    update={
                        "facts": tuple(
                            f
                            for f in evidence.facts
                            if f.subject_id != "offer_offer_other"
                        ),
                    }
                )
            result = ExpertResult(
                operation=operation,
                status=TaskStatus.SUCCESS,
                output=self.inventory.model_dump(mode="json"),
                evidence=evidence,
                started_at=now,
                completed_at=now,
            )
        return OperationBatch(
            results=(result,),
            dispatched_step_ids=(operation.step_id,),
            reused_step_ids=(),
        )


async def _run(executor, *, context=None, action_service=None):
    actions = action_service or _FakeActionService()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,
        answer_producer=GroundedAnswerProducer(None, runtime_mode="off", model=None),
        action_service=actions,
    )
    result = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message=MESSAGE,
        context=context or _merchant_context(),
        deadline_monotonic=10**12,
    )
    return result.result, actions


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("product_ids", [(7, 8), (8, 7)])
async def test_both_prices_grounded_target_independent_of_every_order(
    reverse, product_ids
):
    result, actions = await _run(
        InventoryExecutor(reverse=reverse),
        context=_merchant_context(product_ids),
    )
    assert result.outcome == DialogueOutcome.AWAITING_CONFIRMATION
    assert len(actions.offer_requests) == 1
    request = actions.offer_requests[0]
    assert (request.offer_id, request.expected_version, request.new_price_vnd) == (
        "offer_server_owned",
        6,
        70900,
    )
    assert result.action_cards[0].target.resource_id == "offer_server_owned"
    assert actions.read_inventory_calls == 0  # Use the already-grounded version.
    assert len(result.citations) >= 2
    assert {e.source_id for e in result.evidence} == {
        "sandbox_inventory_offer_other",
        "sandbox_inventory_offer_server_owned",
    }
    assert "75900" in result.answer and "72000" in result.answer
    assert all(claim.citation_ids for claim in result.claims)
    assert [r.capability for r in result.executions] == ["merchant.inventory.read"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options", [{"missing": True}, {"failed": True}, {"no_price": True}]
)
async def test_incomplete_inventory_or_grounding_cannot_create_proposal(options):
    result, actions = await _run(InventoryExecutor(**options))
    assert result.outcome != DialogueOutcome.AWAITING_CONFIRMATION
    assert not result.action_cards
    assert not actions.offer_requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        {"offer_id": "offer_wrong"},
        {"expected_version": 5},
    ],
)
async def test_stale_or_wrong_offer_target_fails_closed(target):
    result, actions = await _run(
        InventoryExecutor(), context=_merchant_context(**target)
    )
    assert result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert result.warnings == ("action_prerequisite_changed",)
    assert result.citations  # Keep the successful reads, even if proposal is blocked.
    assert not actions.offer_requests


def test_target_outside_resolved_products_rejected():
    with pytest.raises(ValidationError, match="merchant target"):
        _merchant_context(product_id=9)


@pytest.mark.asyncio
async def test_combined_model_cannot_drop_read_product_or_choose_target():
    runtime = _ChoiceRuntime(
        {
            "template_id": "merchant_proposal",
            "capabilities": ["merchant.inventory.read", "merchant.offer.propose"],
            "selected_product_ids": [8],
            "candidate_limit": 2,
        }
    )
    with provider_budget_scope(_budget_context()):
        with pytest.raises(ModelPlanRejectedError, match="model_plan_not_authorized"):
            await BoundedV2Planner(model_runtime=runtime, runtime_mode="required").plan(
                MESSAGE,
                _merchant_context(),
            )


@pytest.mark.asyncio
async def test_missing_server_target_still_clarifies_without_reads():
    executor = InventoryExecutor()
    context = _context(
        mode=ConversationMode.MERCHANT, resolved_product_ids=(8, 7), write=True
    )
    result, actions = await _run(executor, context=context)
    assert result.warnings == ("merchant_action_single_product_required",)
    assert executor.calls == 0
    assert not actions.offer_requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"value": -1},
        {"value": True},
        {"unit": "page"},
        {"field": "demo_offer_version", "value": 0, "unit": None},
        {"field": "demo_unapproved_field"},
    ],
)
async def test_inventory_fact_validation_remains_closed(changes):
    tools = V2ReadTools(
        _unused_session_factory,
        catalog_snapshot=CatalogSnapshot("catalog_test", datetime.now(UTC), (1,)),
    )
    evidence = tools._sandbox_action_evidence(
        "merchant.inventory.read",
        InventoryExecutor().inventory,
    )
    evidence = evidence.model_copy(
        update={
            "facts": (evidence.facts[0].model_copy(update=changes),),
        }
    )
    with pytest.raises(GroundingEvidenceError, match="fact_field_"):
        await GroundingVerifier(None, model=None).validate_context(
            evidence,
            allowed_subject_ids=frozenset(
                {"offer_offer_other", "offer_offer_server_owned"}
            ),
        )
