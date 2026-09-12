"""Focused behavior tests for bounded v2 planning and evidence supervision."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts import AuthorizationContext, TaskStatus
from app.shared import ModelCallMetadata, ModelRuntimeError, StructuredModelResult
from app.shared.budget import (
    BudgetCancelledError,
    ProviderBudgetContext,
    provider_budget_scope,
)
from app.v2.authorization import bind_request_authorization
from app.v2.contracts import (
    Citation,
    Claim,
    ConversationMode,
    DialogueOutcome,
    EvidenceKind,
    EvidenceReference,
    SafeExecutionError,
    TurnResult,
    TurnStatus,
)
from app.v2.execution import (
    DurableExecutionError,
    ModelRuntimeExpertReasoner,
    OperationBatch,
)
from app.v2.history import ContextConstraint, HistoryTurn, ModelContext
from app.v2.planning import (
    BoundedV2Planner,
    ModelPlanRejectedError,
    PlanningContext,
    RuntimeDataVersions,
)
from app.v2.registry import (
    KnowledgeExcerpt,
    KnowledgeResult,
    ProductCandidate,
    ProductResult,
    ReviewFinding,
    ReviewResult,
    TrustFinding,
    TrustResult,
)
from app.v2.runtime_contracts import (
    EvidenceExcerpt,
    EvidenceObligation,
    EvidenceObligationKind,
    ExpertResult,
    GroundingResult,
    RuntimeOperation,
    StructuredFact,
    ToolEvidence,
)
from app.v2.supervisor import V2ReadSupervisor, select_grounding_evidence


class _NoopLedger:
    pass


class _ChoiceRuntime:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.last_input_text: str | None = None

    async def generate_structured(self, **kwargs: Any) -> StructuredModelResult[Any]:
        self.calls += 1
        self.last_input_text = kwargs["input_text"]
        if self.error is not None:
            raise self.error
        schema = kwargs["schema"]
        assert self.payload is not None
        return StructuredModelResult(
            value=schema.model_validate(self.payload),
            metadata=_metadata(),
        )


class _FakeOperationExecutor:
    def __init__(
        self,
        *,
        empty_catalog: bool = False,
        fail_capability: str | None = None,
        first_knowledge_unanswerable: bool = False,
        conflicting_price: bool = False,
    ) -> None:
        self.empty_catalog = empty_catalog
        self.fail_capability = fail_capability
        self.first_knowledge_unanswerable = first_knowledge_unanswerable
        self.conflicting_price = conflicting_price
        self.calls: list[tuple[str, ...]] = []
        self.knowledge_calls = 0

    async def execute(
        self,
        *,
        turn_id: str,
        lease_owner: str,
        access: object,
        operations: tuple[RuntimeOperation, ...],
        plan_revision: int,
    ) -> OperationBatch:
        del turn_id, lease_owner, access, plan_revision
        self.calls.append(tuple(operation.capability for operation in operations))
        results = tuple(self._result(operation) for operation in operations)
        return OperationBatch(
            results=results,
            dispatched_step_ids=tuple(result.operation.step_id for result in results),
            reused_step_ids=(),
        )

    def _result(self, operation: RuntimeOperation) -> ExpertResult:
        now = datetime.now(UTC)
        if operation.capability == self.fail_capability:
            return ExpertResult(
                operation=operation,
                status=TaskStatus.FAILED,
                error=SafeExecutionError(
                    code="synthetic_read_failed",
                    message="Synthetic safe failure.",
                    retryable=True,
                ),
                started_at=now,
                completed_at=now,
            )
        if operation.capability == "knowledge.retrieve":
            self.knowledge_calls += 1
            answerable = not (
                self.first_knowledge_unanswerable and self.knowledge_calls == 1
            )
            return _knowledge_result(operation, answerable=answerable)
        product_ids = _operation_product_ids(operation)
        if operation.capability in {"product.catalog.search", "product.rank"}:
            product_ids = () if self.empty_catalog else (1, 2)
        if operation.capability == "product.compare" and not product_ids:
            product_ids = (1, 2)
        if operation.capability in {
            "product.catalog.search",
            "product.rank",
            "product.compare",
            "merchant.catalog.read",
        }:
            return _product_result(
                operation,
                product_ids,
                conflicting_price=self.conflicting_price
                and operation.capability == "product.compare",
            )
        if operation.capability in {"review.retrieve", "review.compare"}:
            return _review_result(operation, product_ids)
        if operation.capability in {"trust.analyze", "trust.compare"}:
            return _trust_result(operation, product_ids)
        raise AssertionError(operation.capability)


class _AnswerProducer:
    def __init__(self) -> None:
        self.calls = 0
        self.allowed_subject_ids: frozenset[str] = frozenset()

    async def produce(self, **kwargs: Any) -> GroundingResult:
        self.calls += 1
        self.allowed_subject_ids = kwargs["allowed_subject_ids"]
        evidence: ToolEvidence = kwargs["evidence"]
        reference = evidence.references[0]
        claim = Claim(
            claim_id="claim_verified",
            text="Kết quả đã được kiểm tra từ dữ liệu nguồn.",
            citation_ids=("citation_verified",),
        )
        citation = Citation(
            citation_id="citation_verified",
            claim_id=claim.claim_id,
            evidence_id=reference.evidence_id,
            span_id=reference.span_id,
            display_label=reference.display_label,
        )
        return GroundingResult(
            outcome=DialogueOutcome.ANSWERED,
            answer=f"{claim.text} {reference.display_label}",
            claims=(claim,),
            citations=(citation,),
            evidence=(reference,),
        )


def test_decimal_facts_round_trip_with_exact_type_and_reject_corruption() -> None:
    fact = StructuredFact(
        fact_id="fact_rating",
        field="snapshot_rating",
        value=Decimal("4.250"),
        unit="rating_5",
        data_version_id="catalog_v1",
        evidence_ids=("evidence_rating",),
    )
    restored = StructuredFact.model_validate(fact.model_dump(mode="json"))
    assert restored.value == Decimal("4.250")
    assert isinstance(restored.value, Decimal)
    payload = fact.model_dump(mode="json")
    payload["value"] = {"decimal": "not-a-decimal"}
    with pytest.raises(ValidationError):
        StructuredFact.model_validate(payload)


@pytest.mark.asyncio
async def test_off_plan_defers_id_bound_comparison_and_binds_operation_key() -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "So sánh giá hai sách lịch sử",
        _context(),
    )
    assert [item.kind for item in planned.obligations] == [
        "catalog",
        "comparison",
        "price",
    ]
    assert [item.capability for item in planned.initial_operations] == [
        "product.catalog.search"
    ]
    assert planned.deferred_capabilities == ("product.compare",)
    operation = planned.initial_operations[0]
    assert operation.data_version_ids == ("catalog_v1",)
    RuntimeOperation.model_validate(operation.model_dump(mode="python"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_limit", "fallback"),
    (("hybrid", 3, None), ("shadow", 5, "shadow_mode")),
)
async def test_valid_model_choice_obeys_hybrid_and_shadow_semantics(
    mode: str,
    expected_limit: int,
    fallback: str | None,
) -> None:
    runtime = _ChoiceRuntime(
        {
            "template_id": "shopper_catalog",
            "capabilities": ["product.catalog.search"],
            "selected_product_ids": [],
            "candidate_limit": 3,
        }
    )
    planner = BoundedV2Planner(model_runtime=runtime, runtime_mode=mode)  # type: ignore[arg-type]
    with provider_budget_scope(_budget_context()):
        planned = await planner.plan("Tìm sách lịch sử", _context())
    assert planned.candidate_limit == expected_limit
    assert planned.fallback_reason == fallback
    assert planned.model_selected_template_id == "shopper_catalog"
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_hybrid_applies_an_authorized_alternate_capability_sequence() -> None:
    runtime = _ChoiceRuntime(
        {
            "template_id": "shopper_knowledge",
            "capabilities": ["product.catalog.search", "knowledge.retrieve"],
            "selected_product_ids": [],
            "candidate_limit": 2,
        }
    )
    planner = BoundedV2Planner(model_runtime=runtime, runtime_mode="hybrid")  # type: ignore[arg-type]
    with provider_budget_scope(_budget_context()):
        planned = await planner.plan("Tìm sách Sapiens", _context())
    assert planned.template_id == "shopper_knowledge"
    assert planned.intent == "knowledge"
    assert planned.desired_capabilities == (
        "product.catalog.search",
        "knowledge.retrieve",
    )
    assert [item.capability for item in planned.initial_operations] == [
        "product.catalog.search"
    ]
    assert planned.deferred_capabilities == ("knowledge.retrieve",)
    assert planned.candidate_limit == 2


@pytest.mark.asyncio
async def test_model_cannot_select_an_unimplemented_registry_template() -> None:
    unavailable = {
        "template_id": "shopper_cart_read",
        "capabilities": ["shopper.cart.read"],
        "selected_product_ids": [],
        "candidate_limit": 5,
    }
    with provider_budget_scope(_budget_context()):
        hybrid = await BoundedV2Planner(
            model_runtime=_ChoiceRuntime(unavailable),
            runtime_mode="hybrid",
        ).plan("Tìm sách Sapiens", _context())
    assert hybrid.template_id == "shopper_catalog"
    assert hybrid.desired_capabilities == ("product.catalog.search",)
    assert hybrid.fallback_reason == "model_plan_not_authorized"

    with provider_budget_scope(_budget_context()):
        with pytest.raises(ModelPlanRejectedError):
            await BoundedV2Planner(
                model_runtime=_ChoiceRuntime(unavailable),
                runtime_mode="required",
            ).plan("Tìm sách Sapiens", _context())


@pytest.mark.asyncio
async def test_mixed_knowledge_review_and_trust_obligations_are_all_retained() -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Tìm sách 'Sapiens', cho biết chủ đề, review và phàn nàn",
        _context(),
    )
    assert [obligation.kind for obligation in planned.obligations] == [
        "catalog",
        "review",
        "trust",
        "knowledge",
    ]
    assert planned.desired_capabilities == (
        "product.catalog.search",
        "review.compare",
        "trust.compare",
        "knowledge.retrieve",
    )
    assert planned.initial_operations[0].parameters["query"] == "Sapiens"


@pytest.mark.asyncio
async def test_catalog_query_extracts_entity_and_keeps_python_price_constraint() -> (
    None
):
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Tìm sách Sapiens dưới 200 nghìn, cho biết giá và review",
        _context(),
    )
    operation = planned.initial_operations[0]
    assert operation.parameters["query"] == "Sapiens"
    assert operation.parameters["max_price_vnd"] == 200_000


@pytest.mark.asyncio
async def test_history_supplies_products_and_constraints_when_current_absent() -> None:
    context = _context().model_copy(
        update={
            "model_context": ModelContext(
                active_constraints=(
                    ContextConstraint(key="max_price_vnd", value=180_000),
                ),
                referenced_product_ids=(41, 42),
            )
        }
    )
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "So sánh chúng",
        context,
    )
    comparison = next(
        item
        for item in planned.initial_operations
        if item.capability == "product.compare"
    )
    assert comparison.parameters["product_ids"] == [41, 42]
    assert planned.max_price_vnd == 180_000


@pytest.mark.asyncio
async def test_max_budget_preference_constrains_catalog_when_price_is_absent() -> None:
    context = _context().model_copy(
        update={
            "model_context": ModelContext(
                active_constraints=(
                    ContextConstraint(key="max_budget_vnd", value=175_000),
                )
            )
        }
    )
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Tìm sách lịch sử",
        context,
    )
    assert planned.max_price_vnd == 175_000
    assert planned.initial_operations[0].parameters["max_price_vnd"] == 175_000


@pytest.mark.asyncio
async def test_current_products_and_one_sided_price_override_historical_context() -> (
    None
):
    context = _context(resolved_product_ids=(7, 8)).model_copy(
        update={
            "model_context": ModelContext(
                active_constraints=(
                    ContextConstraint(key="min_price_vnd", value=50_000),
                    ContextConstraint(key="max_price_vnd", value=100_000),
                ),
                referenced_product_ids=(41, 42),
            )
        }
    )
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "So sánh 2 cuốn trên 300 nghìn",
        context,
    )
    assert planned.min_price_vnd == 300_000
    assert planned.max_price_vnd is None
    comparison = next(
        operation
        for operation in planned.initial_operations
        if operation.capability == "product.compare"
    )
    assert comparison.parameters["product_ids"] == [7, 8]


@pytest.mark.asyncio
async def test_explicit_current_limit_caps_historical_product_prefix() -> None:
    context = _context().model_copy(
        update={
            "model_context": ModelContext(
                referenced_product_ids=(41, 42, 43),
            )
        }
    )
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "So sánh 2 cuốn",
        context,
    )
    comparison = next(
        operation
        for operation in planned.initial_operations
        if operation.capability == "product.compare"
    )
    assert planned.candidate_limit == 2
    assert comparison.parameters["product_ids"] == [41, 42]


@pytest.mark.asyncio
async def test_model_history_projection_is_bounded_without_assistant_material() -> None:
    now = datetime.now(UTC)
    history = HistoryTurn(
        turn_id="turn_history_projection",
        client_turn_id="client-history-projection",
        status=TurnStatus.COMPLETED,
        outcome=DialogueOutcome.ANSWERED,
        user_message="Earlier user filter " + "x" * 1_000,
        assistant_result=TurnResult(
            outcome=DialogueOutcome.ANSWERED,
            answer="SECRET_ASSISTANT_ANSWER",
        ),
        created_at=now,
        completed_at=now,
    )
    runtime = _ChoiceRuntime(
        {
            "template_id": "shopper_catalog",
            "capabilities": ["product.catalog.search"],
            "selected_product_ids": [41],
            "candidate_limit": 1,
        }
    )
    context = _context().model_copy(
        update={
            "model_context": ModelContext(
                recent_turns=(history,),
                active_constraints=(
                    ContextConstraint(key="catalog_query", value="history"),
                ),
                referenced_product_ids=(41,),
            )
        }
    )
    with provider_budget_scope(_budget_context()):
        await BoundedV2Planner(
            model_runtime=runtime,
            runtime_mode="hybrid",
        ).plan("Tìm sách", context)
    assert runtime.last_input_text is not None
    assert len(runtime.last_input_text.encode("utf-8")) < 12_000
    assert "planning context only" in runtime.last_input_text
    assert "SECRET_ASSISTANT_ANSWER" not in runtime.last_input_text
    assert '"claims"' not in runtime.last_input_text
    assert '"citations"' not in runtime.last_input_text
    assert '"assistant_result"' not in runtime.last_input_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    (
        ("Tìm sách dưới 1,5 triệu", 1_500_000),
        ("Tìm sách dưới 1.5tr", 1_500_000),
        ("Tìm sách dưới 2,5k", 2_500),
        ("Tìm sách dưới 150.000đ", 150_000),
        ("Tìm sách dưới 1.500.000 VND", 1_500_000),
    ),
)
async def test_price_constraints_preserve_decimal_and_grouping_semantics(
    message: str,
    expected: int,
) -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(message, _context())
    assert planned.clarification_code is None
    assert planned.max_price_vnd == expected
    assert planned.initial_operations[0].parameters["max_price_vnd"] == expected


@pytest.mark.asyncio
async def test_ambiguous_unsuffixed_decimal_requires_clarification() -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Tìm sách dưới 1,5",
        _context(),
    )
    assert planned.clarification_code == "price_constraint_ambiguous"
    assert planned.max_price_vnd is None
    assert planned.initial_operations == ()


@pytest.mark.asyncio
async def test_single_book_recommendation_does_not_require_two_candidates() -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Gợi ý 1 cuốn sách Sapiens",
        _context(),
    )
    assert "comparison" not in {item.kind for item in planned.obligations}
    assert planned.desired_capabilities == (
        "product.rank",
        "review.retrieve",
        "trust.analyze",
    )


@pytest.mark.asyncio
async def test_hybrid_rejects_invented_ids_and_required_keeps_failure_visible() -> None:
    malicious = {
        "template_id": "shopper_compare",
        "capabilities": ["product.catalog.search", "product.compare"],
        "selected_product_ids": [999999],
        "candidate_limit": 5,
    }
    context = _context(resolved_product_ids=(1, 2))
    with provider_budget_scope(_budget_context()):
        hybrid = await BoundedV2Planner(
            model_runtime=_ChoiceRuntime(malicious),
            runtime_mode="hybrid",
        ).plan("So sánh hai sách", context)
    assert hybrid.fallback_reason == "model_plan_not_authorized"
    comparison = next(
        operation
        for operation in hybrid.initial_operations
        if operation.capability == "product.compare"
    )
    assert comparison.parameters["product_ids"] == [1, 2]

    with provider_budget_scope(_budget_context()):
        with pytest.raises(ModelPlanRejectedError):
            await BoundedV2Planner(
                model_runtime=_ChoiceRuntime(malicious),
                runtime_mode="required",
            ).plan("So sánh hai sách", context)


@pytest.mark.asyncio
async def test_budget_cancellation_is_never_converted_to_model_fallback() -> None:
    planner = BoundedV2Planner(
        model_runtime=_ChoiceRuntime(error=BudgetCancelledError("cancelled")),
        runtime_mode="hybrid",
    )
    with provider_budget_scope(_budget_context()):
        with pytest.raises(BudgetCancelledError):
            await planner.plan("Tìm sách", _context())


@pytest.mark.asyncio
async def test_required_runtime_failure_is_not_deterministic_success() -> None:
    error = ModelRuntimeError("model_timeout", _metadata(status="failed"))
    planner = BoundedV2Planner(
        model_runtime=_ChoiceRuntime(error=error),
        runtime_mode="required",
    )
    with provider_budget_scope(_budget_context()):
        with pytest.raises(ModelRuntimeError):
            await planner.plan("Tìm sách", _context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_fallback"),
    (("hybrid", None), ("shadow", "shadow_mode")),
)
async def test_expert_reasoning_selects_only_existing_checked_ids(
    mode: str,
    expected_fallback: str | None,
) -> None:
    operation = (
        await BoundedV2Planner(runtime_mode="off").plan(
            "Tìm sách Sapiens", _context(resolved_product_ids=(1,))
        )
    ).initial_operations[0]
    deterministic = _product_result(operation, (1,))
    fact_id = deterministic.evidence.facts[0].fact_id
    evidence_id = deterministic.evidence.references[0].evidence_id
    runtime = _ChoiceRuntime(
        {
            "selected_fact_ids": [fact_id],
            "selected_evidence_ids": [evidence_id],
        }
    )
    reasoner = ModelRuntimeExpertReasoner(
        runtime,  # type: ignore[arg-type]
        runtime_mode=mode,  # type: ignore[arg-type]
        model="test-model",
    )
    with provider_budget_scope(_budget_context()):
        enriched = await reasoner.enrich(deterministic)
    assert enriched.output == deterministic.output
    assert enriched.evidence == deterministic.evidence
    assert enriched.selected_fact_ids == (fact_id,)
    assert enriched.selected_evidence_ids == (evidence_id,)
    assert enriched.reasoning_fallback_reason == expected_fallback


@pytest.mark.asyncio
async def test_required_expert_rejects_unknown_fact_instead_of_succeeding() -> None:
    operation = (
        await BoundedV2Planner(runtime_mode="off").plan(
            "Tìm sách Sapiens", _context(resolved_product_ids=(1,))
        )
    ).initial_operations[0]
    deterministic = _product_result(operation, (1,))
    reasoner = ModelRuntimeExpertReasoner(
        _ChoiceRuntime(
            {
                "selected_fact_ids": ["fact_unknown"],
                "selected_evidence_ids": [],
            }
        ),  # type: ignore[arg-type]
        runtime_mode="required",
        model="test-model",
    )
    with provider_budget_scope(_budget_context()):
        with pytest.raises(
            DurableExecutionError, match="expert_selection_not_authorized"
        ):
            await reasoner.enrich(deterministic)


@pytest.mark.asyncio
async def test_expert_selection_filters_synthesis_but_restores_required_evidence() -> (
    None
):
    operation = (
        await BoundedV2Planner(runtime_mode="off").plan(
            "Tìm sách Sapiens", _context(resolved_product_ids=(1,))
        )
    ).initial_operations[0]
    deterministic = _product_result(operation, (1,))
    selected = deterministic.model_copy(
        update={
            "selected_fact_ids": (deterministic.evidence.facts[0].fact_id,),
            "selected_evidence_ids": (
                deterministic.evidence.references[0].evidence_id,
            ),
        }
    )
    implicit = EvidenceObligation(
        obligation_id=operation.obligation_ids[0],
        kind=EvidenceObligationKind.CATALOG,
        description="implicit catalog context",
        explicit=False,
    )
    filtered = select_grounding_evidence((selected,), (implicit,))
    assert len(filtered.facts) == 1
    explicit = implicit.model_copy(update={"explicit": True})
    restored = select_grounding_evidence((selected,), (explicit,))
    assert restored == deterministic.evidence


@pytest.mark.asyncio
async def test_supervisor_runs_only_two_new_continuation_reads() -> None:
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
    )
    computation = await supervisor.run_claimed(
        turn_id="turn_recommendation",
        lease_owner="worker_test",
        message="Gợi ý sách có review tốt và ít phàn nàn",
        context=_context(),
        deadline_monotonic=10**12,
    )
    assert executor.calls == [
        ("product.rank",),
        ("review.compare", "trust.compare"),
    ]
    assert computation.result.outcome == DialogueOutcome.ANSWERED
    assert computation.result.plan is not None
    assert len(computation.result.plan.revisions) == 1
    assert computation.result.plan.revisions[0].added_read_step_ids == ()
    assert len(computation.result.executions) == 3
    assert producer.allowed_subject_ids == frozenset({"product_1", "product_2"})


@pytest.mark.asyncio
async def test_dependent_reads_are_initial_and_leave_revision_for_new_evidence() -> (
    None
):
    executor = _FakeOperationExecutor(first_knowledge_unanswerable=True)
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=_AnswerProducer(),
    )
    computation = await supervisor.run_claimed(
        turn_id="turn_staged_mixed",
        lease_owner="worker_test",
        message=("Tìm sách 'Sapiens', so sánh, cho biết chủ đề, review và phàn nàn"),
        context=_context(),
        deadline_monotonic=10**12,
    )
    assert executor.calls == [
        ("product.catalog.search",),
        (
            "product.compare",
            "review.compare",
            "trust.compare",
            "knowledge.retrieve",
        ),
        ("knowledge.retrieve",),
    ]
    assert computation.result.plan is not None
    assert len(computation.result.plan.revisions) == 2
    assert computation.result.plan.revisions[1].added_read_step_ids == ("step_006",)


@pytest.mark.asyncio
async def test_no_candidates_returns_clarification_without_drafting() -> None:
    executor = _FakeOperationExecutor(empty_catalog=True)
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
    )
    computation = await supervisor.run_claimed(
        turn_id="turn_empty",
        lease_owner="worker_test",
        message="So sánh sách lịch sử",
        context=_context(),
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert producer.calls == 0
    assert executor.calls == [("product.catalog.search",)]


@pytest.mark.asyncio
async def test_missing_explicit_evidence_abstains_instead_of_shortening_plan() -> None:
    executor = _FakeOperationExecutor(fail_capability="trust.compare")
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
    )
    computation = await supervisor.run_claimed(
        turn_id="turn_missing_trust",
        lease_owner="worker_test",
        message="Gợi ý sách và kiểm tra phàn nàn",
        context=_context(),
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome == DialogueOutcome.ABSTAINED
    assert "evidence_incomplete" in computation.result.warnings
    assert producer.calls == 0


@pytest.mark.asyncio
async def test_knowledge_expansion_is_bounded_to_two_total_retrievals() -> None:
    executor = _FakeOperationExecutor(first_knowledge_unanswerable=True)
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
    )
    computation = await supervisor.run_claimed(
        turn_id="turn_knowledge",
        lease_owner="worker_test",
        message="Sách này nói về chủ đề gì?",
        context=_context(),
        deadline_monotonic=10**12,
    )
    assert executor.calls == [("knowledge.retrieve",), ("knowledge.retrieve",)]
    assert executor.knowledge_calls == 2
    assert computation.knowledge_retrievals == 2
    assert computation.result.outcome == DialogueOutcome.ANSWERED


@pytest.mark.asyncio
async def test_user_candidate_request_above_cap_needs_clarification_before_reads() -> (
    None
):
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Gợi ý 12 cuốn sách lịch sử",
        _context(),
    )
    assert planned.clarification_code == "candidate_limit_exceeds_five"
    assert planned.initial_operations == ()


def _context(
    *,
    mode: ConversationMode = ConversationMode.SHOPPER,
    resolved_product_ids: tuple[int, ...] = (),
) -> PlanningContext:
    scopes = {"ecommerce.read"}
    if mode == ConversationMode.MERCHANT:
        scopes.add("merchant.read")
    authorization = AuthorizationContext(
        principal_id="principal_test",
        tenant_id="tenant_test",
        scopes=frozenset(scopes),
    )
    return PlanningContext(
        access=bind_request_authorization(authorization, mode),
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_v1",
            corpus_version_id="corpus_v1",
            index_manifest_id="index_v1",
        ),
        resolved_product_ids=resolved_product_ids,
    )


def _budget_context() -> ProviderBudgetContext:
    return ProviderBudgetContext(
        ledger=_NoopLedger(),  # type: ignore[arg-type]
        scope_id="turn_budget_test",
        purpose="chat",
    )


def _metadata(*, status: str = "success") -> ModelCallMetadata:
    return ModelCallMetadata(
        call_id="mcall_00000000000000000000000000000001",
        stage="v2_planning",
        agent_id="supervisor",
        model="test-model",
        status=status,  # type: ignore[arg-type]
        duration_ms=1,
        attempts=1,
        error_code="model_timeout" if status == "failed" else None,
    )


def _reference(
    operation: RuntimeOperation, *, knowledge: bool = False
) -> EvidenceReference:
    version = (
        operation.data_version_ids[1] if knowledge else operation.data_version_ids[0]
    )
    return EvidenceReference(
        evidence_id=f"evidence_{operation.step_id}",
        source_id=f"source_{operation.step_id}",
        source_version_id=version,
        chunk_id=f"chunk_{operation.step_id}",
        span_id=f"span_{operation.step_id}",
        display_label="[C1]",
        kind=EvidenceKind.KNOWLEDGE if knowledge else EvidenceKind.CATALOG,
        title="Synthetic source",
        observed_at=datetime.now(UTC),
    )


def _product_result(
    operation: RuntimeOperation,
    product_ids: tuple[int, ...],
    *,
    conflicting_price: bool = False,
) -> ExpertResult:
    reference = _reference(operation)
    products = tuple(
        ProductCandidate(
            product_id=product_id,
            title=f"Book {product_id}",
            price_vnd=200_000 if conflicting_price else 100_000 + product_id,
            catalog_version_id=operation.data_version_ids[0],
        )
        for product_id in product_ids
    )
    facts = tuple(
        StructuredFact(
            fact_id=f"fact_{operation.step_id}_{product_id}",
            subject_id=f"product_{product_id}",
            field="snapshot_price_vnd",
            value=200_000 if conflicting_price else 100_000 + product_id,
            unit="VND",
            data_version_id=operation.data_version_ids[0],
            evidence_ids=(reference.evidence_id,),
        )
        for product_id in product_ids
    )
    output = ProductResult(products=products, evidence=(reference,))
    return _success(
        operation,
        output.model_dump(mode="json"),
        ToolEvidence(facts=facts, references=(reference,)),
    )


def _review_result(
    operation: RuntimeOperation, product_ids: tuple[int, ...]
) -> ExpertResult:
    reference = _reference(operation)
    output = ReviewResult(
        findings=tuple(
            ReviewFinding(
                product_id=product_id,
                review_count=5,
                average_rating=4.0,
                summary="Synthetic review summary",
                evidence_ids=(reference.evidence_id,),
            )
            for product_id in product_ids
        )
    )
    facts = tuple(
        StructuredFact(
            fact_id=f"fact_review_{product_id}",
            subject_id=f"product_{product_id}",
            field="sampled_review_count",
            value=5,
            unit="review",
            data_version_id=operation.data_version_ids[0],
            evidence_ids=(reference.evidence_id,),
        )
        for product_id in product_ids
    )
    return _success(
        operation,
        output.model_dump(mode="json"),
        ToolEvidence(facts=facts, references=(reference,)),
    )


def _trust_result(
    operation: RuntimeOperation, product_ids: tuple[int, ...]
) -> ExpertResult:
    reference = _reference(operation)
    output = TrustResult(
        findings=tuple(
            TrustFinding(
                product_id=product_id,
                complaint_count=1,
                summary="Synthetic bounded heuristic",
                evidence_ids=(reference.evidence_id,),
            )
            for product_id in product_ids
        )
    )
    facts = tuple(
        StructuredFact(
            fact_id=f"fact_trust_{product_id}",
            subject_id=f"product_{product_id}",
            field="complaint_count",
            value=1,
            unit="complaint",
            data_version_id=operation.data_version_ids[0],
            evidence_ids=(reference.evidence_id,),
        )
        for product_id in product_ids
    )
    return _success(
        operation,
        output.model_dump(mode="json"),
        ToolEvidence(facts=facts, references=(reference,)),
    )


def _knowledge_result(operation: RuntimeOperation, *, answerable: bool) -> ExpertResult:
    reference = _reference(operation, knowledge=True)
    excerpts = (
        (
            KnowledgeExcerpt(
                evidence_id=reference.evidence_id,
                source_id=reference.source_id,
                source_version_id=reference.source_version_id,
                chunk_id=reference.chunk_id or "chunk_missing",
                span_id=reference.span_id or "span_missing",
                excerpt="Synthetic exact knowledge excerpt.",
                score=0.9,
            ),
        )
        if answerable
        else ()
    )
    output = KnowledgeResult(
        corpus_version_id=operation.data_version_ids[1],
        excerpts=excerpts,
        answerable=answerable,
    )
    evidence = ToolEvidence(
        excerpts=(
            (
                EvidenceExcerpt(
                    evidence_id=reference.evidence_id,
                    source_id=reference.source_id,
                    source_version_id=reference.source_version_id,
                    chunk_id=reference.chunk_id or "chunk_missing",
                    span_id=reference.span_id or "span_missing",
                    exact_text="Synthetic exact knowledge excerpt.",
                ),
            )
            if answerable
            else ()
        ),
        references=(reference,) if answerable else (),
    )
    return _success(operation, output.model_dump(mode="json"), evidence)


def _success(
    operation: RuntimeOperation,
    output: dict[str, Any],
    evidence: ToolEvidence,
) -> ExpertResult:
    now = datetime.now(UTC)
    return ExpertResult(
        operation=operation,
        status=TaskStatus.SUCCESS,
        output=output,
        evidence=evidence,
        started_at=now,
        completed_at=now,
    )


def _operation_product_ids(operation: RuntimeOperation) -> tuple[int, ...]:
    value = operation.parameters.get("product_ids", [])
    assert isinstance(value, list)
    return tuple(int(item) for item in value)


def _unused_session_factory() -> Any:
    raise AssertionError("off-mode supervisor must not query a provider budget scope")
