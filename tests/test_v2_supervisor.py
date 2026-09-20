"""Focused behavior tests for bounded v2 planning and evidence supervision."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from app.contracts import AuthorizationContext, TaskStatus
from app.shared import ModelCallMetadata, ModelRuntimeError, StructuredModelResult
from app.shared.budget import (
    GENERATION_OUTPUT_TOKEN_LIMIT,
    BudgetCancelledError,
    ProviderBudgetContext,
    provider_budget_scope,
)
from app.v2.actions import ActionConflictError, ActionServiceError
from app.v2.authorization import (
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceBinding,
    ResourceNotFoundError,
    bind_request_authorization,
)
from app.v2.contracts import (
    ActionCard,
    ActionChange,
    ActionKind,
    ActionStatus,
    ActionTarget,
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
    _expert_model_input,
)
from app.v2.history import ContextConstraint, HistoryTurn, ModelContext
from app.v2.planning import (
    BoundedV2Planner,
    ModelPlanRejectedError,
    PlanningContext,
    PlanningError,
    RuntimeDataVersions,
    context_constraints_from_message,
)
from app.v2.registry import (
    CartReadInput,
    CartResult,
    CheckoutInput,
    KnowledgeExcerpt,
    KnowledgeResult,
    MerchantOffer,
    MerchantOfferProposalInput,
    MerchantReadInput,
    MerchantReadResult,
    ProductCandidate,
    ProductResult,
    ProposalResult,
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
        self.last_instructions: str | None = None
        self.last_max_output_tokens: int | None = None

    async def generate_structured(self, **kwargs: Any) -> StructuredModelResult[Any]:
        self.calls += 1
        self.last_input_text = kwargs["input_text"]
        self.last_instructions = kwargs["instructions"]
        self.last_max_output_tokens = kwargs["max_output_tokens"]
        if self.error is not None:
            raise self.error
        schema = kwargs["schema"]
        assert self.payload is not None
        return StructuredModelResult(
            value=schema.model_validate(self.payload),
            metadata=_metadata(),
        )


class _FakeActionService:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.read_cart_calls = 0
        self.read_inventory_calls = 0
        self.checkout_requests: list[CheckoutInput] = []
        self.offer_requests: list[MerchantOfferProposalInput] = []
        self.proposal_lease_owners: list[str] = []
        self.recovery_requests: list[
            tuple[AuthorizationContext, str, str, ConversationMode]
        ] = []
        self.fail_at = fail_at
        self.error = error

    def _raise_if_configured(self, stage: str) -> None:
        if self.fail_at == stage:
            assert self.error is not None
            raise self.error

    def read_cart(
        self,
        authorization: AuthorizationContext,
        request: CartReadInput,
    ) -> CartResult:
        del authorization, request
        self.read_cart_calls += 1
        self._raise_if_configured("read_cart")
        return CartResult(
            cart_id="cart_server_owned",
            version=4,
            items=(),
            total_price_vnd=250_000,
        )

    def propose_checkout(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        lease_owner: str,
        request: CheckoutInput,
    ) -> ProposalResult:
        del authorization
        assert conversation_id == "conversation_action"
        assert turn_id == "turn_action"
        self.proposal_lease_owners.append(lease_owner)
        self.checkout_requests.append(request)
        self._raise_if_configured("propose_checkout")
        return ProposalResult(
            action=_action_card(
                ActionKind.CHECKOUT,
                target_type="cart",
                target_id=request.cart_id,
                target_version=request.expected_version,
            )
        )

    def recover_expired_turn_proposal(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        mode: ConversationMode,
    ) -> bool:
        self.recovery_requests.append((authorization, conversation_id, turn_id, mode))
        return True

    def read_inventory(
        self,
        authorization: AuthorizationContext,
        request: MerchantReadInput,
    ) -> MerchantReadResult:
        del authorization
        self.read_inventory_calls += 1
        self._raise_if_configured("read_inventory")
        assert request.product_ids == (7,)
        return MerchantReadResult(
            offers=(
                MerchantOffer(
                    offer_id="offer_server_owned",
                    product_id=7,
                    price_vnd=100_000,
                    available_quantity=8,
                    version=6,
                ),
            ),
            snapshot_version_id="sandbox_snapshot_test",
        )

    def propose_offer_change(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        lease_owner: str,
        request: MerchantOfferProposalInput,
    ) -> ProposalResult:
        del authorization
        assert conversation_id == "conversation_action"
        assert turn_id == "turn_action"
        self.proposal_lease_owners.append(lease_owner)
        self.offer_requests.append(request)
        self._raise_if_configured("propose_offer")
        kind = (
            ActionKind.MERCHANT_PRICE_CHANGE
            if request.new_price_vnd is not None
            else ActionKind.MERCHANT_INVENTORY_CHANGE
        )
        return ProposalResult(
            action=_action_card(
                kind,
                target_type="offer",
                target_id=request.offer_id,
                target_version=request.expected_version,
            )
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
async def test_planner_preflights_full_structured_payload_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedPlanChoice(BaseModel):
        padding: str = Field(default="", description="x" * 12_000)

    runtime = _ChoiceRuntime(
        {
            "template_id": "shopper_catalog",
            "capabilities": ["product.catalog.search"],
            "selected_product_ids": [],
            "candidate_limit": 3,
        }
    )
    monkeypatch.setattr("app.v2.planning.ModelPlanChoice", OversizedPlanChoice)

    with provider_budget_scope(_budget_context()):
        with pytest.raises(PlanningError, match="planning_model_input_too_large"):
            await BoundedV2Planner(
                model_runtime=runtime, runtime_mode="required"
            ).plan("Tìm sách lịch sử", _context())

    assert runtime.calls == 0


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
async def test_deterministic_checkout_is_proposal_only() -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Thanh toán giỏ hàng",
        _context(write=True),
    )
    assert planned.clarification_code is None
    assert planned.proposal is not None
    assert planned.proposal.kind == "checkout"
    assert planned.desired_capabilities == (
        "shopper.cart.read",
        "shopper.checkout.preview",
        "shopper.checkout.propose",
    )
    assert planned.initial_operations == ()
    assert planned.deferred_capabilities == ()
    assert context_constraints_from_message("Thanh toán giỏ hàng") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "kind", "price", "delta"),
    [
        ("Đổi giá thành 200.000 VND", "merchant_price", 200_000, None),
        ("Tăng tồn kho thêm 5", "merchant_stock", None, 5),
        ("Điều chỉnh tồn kho -3", "merchant_stock", None, -3),
    ],
)
async def test_exact_merchant_proposal_semantics(
    message: str,
    kind: str,
    price: int | None,
    delta: int | None,
) -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        message,
        _context(
            mode=ConversationMode.MERCHANT,
            resolved_product_ids=(7,),
            write=True,
        ),
    )
    assert planned.clarification_code is None
    assert planned.proposal is not None
    assert planned.proposal.kind == kind
    assert planned.proposal.product_id == 7
    assert planned.proposal.new_price_vnd == price
    assert planned.proposal.quantity_delta == delta
    assert planned.initial_operations == ()


@pytest.mark.asyncio
async def test_embedded_single_product_price_mutation_is_a_proposal() -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        (
            "Cho biết giá hiện tại, rồi đặt mức bán của sản phẩm đã chọn "
            "ở 139.300 đồng."
        ),
        _context(
            mode=ConversationMode.MERCHANT,
            resolved_product_ids=(7,),
            write=True,
        ),
    )

    assert planned.clarification_code is None
    assert planned.proposal is not None
    assert planned.proposal.kind == "merchant_price"
    assert planned.proposal.product_id == 7
    assert planned.proposal.new_price_vnd == 139_300
    assert planned.initial_operations == ()


@pytest.mark.asyncio
async def test_informational_price_wording_remains_a_read() -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        "Cho biết giá bán hiện tại của sản phẩm đang ở 40.000 đồng có đúng không?",
        _context(
            mode=ConversationMode.MERCHANT,
            resolved_product_ids=(7,),
            write=True,
        ),
    )

    assert planned.clarification_code is None
    assert planned.proposal is None
    assert planned.initial_operations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "mode", "product_ids", "write", "code"),
    [
        (
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (),
            True,
            "merchant_action_product_required",
        ),
        (
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7, 8),
            True,
            "merchant_action_single_product_required",
        ),
        (
            "Đổi giá thành 100 hoặc 200 VND",
            ConversationMode.MERCHANT,
            (7,),
            True,
            "merchant_price_value_required",
        ),
        (
            "Đổi giá",
            ConversationMode.MERCHANT,
            (7,),
            True,
            "merchant_price_value_required",
        ),
        (
            "Điều chỉnh tồn kho",
            ConversationMode.MERCHANT,
            (7,),
            True,
            "merchant_stock_delta_required",
        ),
        (
            "Tăng tồn kho thêm 0",
            ConversationMode.MERCHANT,
            (7,),
            True,
            "merchant_stock_delta_nonzero",
        ),
        (
            "Thanh toán giỏ hàng",
            ConversationMode.MERCHANT,
            (),
            True,
            "action_mode_mismatch",
        ),
        (
            "Thanh toán giỏ hàng",
            ConversationMode.SHOPPER,
            (),
            False,
            "action_write_permission_required",
        ),
    ],
)
async def test_action_ambiguity_target_mode_and_permission_clarify(
    message: str,
    mode: ConversationMode,
    product_ids: tuple[int, ...],
    write: bool,
    code: str,
) -> None:
    context = _context(
        mode=mode,
        resolved_product_ids=product_ids,
        write=write,
    )
    planned = await BoundedV2Planner(runtime_mode="off").plan(message, context)
    assert planned.proposal is None
    assert planned.clarification_code == code
    assert planned.initial_operations == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "message", "scopes", "product_ids"),
    [
        (
            ConversationMode.SHOPPER,
            "Thanh toán giỏ hàng",
            frozenset({"ecommerce.write"}),
            (),
        ),
        (
            ConversationMode.MERCHANT,
            "Đổi giá thành 200000 VND",
            frozenset({"ecommerce.read", "merchant.write"}),
            (7,),
        ),
    ],
)
async def test_proposal_requires_complete_mode_scope_set(
    mode: ConversationMode,
    message: str,
    scopes: frozenset[str],
    product_ids: tuple[int, ...],
) -> None:
    planned = await BoundedV2Planner(runtime_mode="off").plan(
        message,
        _context_with_scopes(mode, scopes, product_ids),
    )
    assert planned.proposal is None
    assert planned.clarification_code == "action_write_permission_required"
    assert planned.initial_operations == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "yes",
        "no",
        "đồng ý",
        "xác nhận",
        "yes please",
        "No thanks",
        "đồng ý nhé",
        "xác nhận ạ",
    ],
)
async def test_plain_confirmation_bypasses_model_and_action_service(
    message: str,
) -> None:
    runtime = _ChoiceRuntime(error=AssertionError("model must not run"))
    planner = BoundedV2Planner(
        model_runtime=runtime,  # type: ignore[arg-type]
        runtime_mode="required",
    )
    action_service = _FakeActionService()
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=planner,
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=action_service,  # type: ignore[arg-type]
    )
    computation = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message=message,
        context=_context(write=True),
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert computation.result.warnings == ("action_confirmation_endpoint_required",)
    assert runtime.calls == 0
    assert executor.calls == []
    assert producer.calls == 0
    assert action_service.proposal_lease_owners == []
    assert action_service.read_cart_calls == 0
    assert action_service.read_inventory_calls == 0
    assert action_service.checkout_requests == []
    assert action_service.offer_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "product_ids", "write", "code"),
    [
        (
            "Tôi đang rà soát hai mặt hàng “Lên Tàu Cùng Socrates” và “Nghệ "
            "Thuật PR Bản Thân”. Cho biết giá hiện tại của từng cuốn, rồi đặt "
            "mức bán của “Lên Tàu Cùng Socrates” ở 139.300 đồng.",
            (96, 99),
            True,
            "merchant_action_single_product_required",
        ),
        (
            "Trong danh mục cửa hàng, “Những Giấc Mơ Ở Hiệu Sách Morisaki” "
            "và “Economix” đang được niêm yết. Tôi cần chuyển giá bán của "
            "“Những Giấc Mơ Ở Hiệu Sách Morisaki” xuống 40.000 đồng.",
            (108, 109),
            False,
            "action_write_permission_required",
        ),
        (
            "Tồn kho hiện có “Những người phụ nữ bé nhỏ” và “Quốc Gia Khởi "
            "Nghiệp”. Hãy áp dụng ngay một ưu đãi cho “Những người phụ nữ bé "
            "nhỏ”.",
            (38, 44),
            True,
            "merchant_offer_execution_requires_confirmed_proposal",
        ),
    ],
)
async def test_embedded_merchant_actions_short_circuit_without_dispatch(
    message: str,
    product_ids: tuple[int, ...],
    write: bool,
    code: str,
) -> None:
    runtime = _ChoiceRuntime(error=AssertionError("model must not run"))
    action_service = _FakeActionService()
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(
            model_runtime=runtime,  # type: ignore[arg-type]
            runtime_mode="required",
        ),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=action_service,  # type: ignore[arg-type]
    )

    computation = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message=message,
        context=_context(
            mode=ConversationMode.MERCHANT,
            resolved_product_ids=product_ids,
            write=write,
        ),
        deadline_monotonic=10**12,
    )

    assert computation.result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert computation.result.warnings == (code,)
    assert computation.result.action_cards == ()
    assert runtime.calls == 0
    assert executor.calls == []
    assert producer.calls == 0
    assert action_service.read_cart_calls == 0
    assert action_service.read_inventory_calls == 0
    assert action_service.checkout_requests == []
    assert action_service.offer_requests == []


def test_supervisor_forwards_atomic_recovery_and_short_circuits_without_service() -> (
    None
):
    context = _context(write=True)
    planner = BoundedV2Planner(runtime_mode="off")
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    without_actions = V2ReadSupervisor(
        _unused_session_factory,
        planner=planner,
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
    )
    assert (
        without_actions.recover_expired_turn_proposal(
            conversation_id="conversation_action",
            turn_id="turn_action",
            access=context.access,
        )
        is False
    )

    actions = _FakeActionService()
    with_actions = V2ReadSupervisor(
        _unused_session_factory,
        planner=planner,
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=actions,  # type: ignore[arg-type]
    )
    assert (
        with_actions.recover_expired_turn_proposal(
            conversation_id="conversation_action",
            turn_id="turn_action",
            access=context.access,
        )
        is True
    )
    assert actions.recovery_requests == [
        (
            AuthorizationContext(
                tenant_id=context.access.binding.tenant_id,
                principal_id=context.access.binding.principal_id,
                scopes=context.access.scopes,
            ),
            "conversation_action",
            "turn_action",
            ConversationMode.SHOPPER,
        )
    ]


@pytest.mark.asyncio
async def test_substantive_request_after_yes_still_reaches_model_planning() -> None:
    runtime = _ChoiceRuntime(error=AssertionError("model planning reached"))
    planner = BoundedV2Planner(
        model_runtime=runtime,  # type: ignore[arg-type]
        runtime_mode="required",
    )
    with provider_budget_scope(_budget_context()):
        with pytest.raises(AssertionError, match="model planning reached"):
            await planner.plan("yes, recommend books", _context())
    assert runtime.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_mode", ["hybrid", "required"])
async def test_model_can_only_copy_single_authorized_proposal_option(
    runtime_mode: str,
) -> None:
    runtime = _ChoiceRuntime(
        {
            "template_id": "merchant_proposal",
            "capabilities": [
                "merchant.inventory.read",
                "merchant.offer.propose",
            ],
            "selected_product_ids": [7],
            "candidate_limit": 1,
        }
    )
    with provider_budget_scope(_budget_context()):
        planned = await BoundedV2Planner(
            model_runtime=runtime,  # type: ignore[arg-type]
            runtime_mode=runtime_mode,  # type: ignore[arg-type]
        ).plan(
            "Đổi giá thành 200000 VND",
            _context(
                mode=ConversationMode.MERCHANT,
                resolved_product_ids=(7,),
                write=True,
            ),
        )
    assert planned.proposal is not None
    assert planned.initial_operations == ()
    assert planned.model_selected_template_id == "merchant_proposal"
    assert runtime.last_input_text is not None
    assert "merchant.offer.execute" not in runtime.last_input_text
    assert runtime.last_instructions is not None
    assert "one supplied authorized proposal template" in runtime.last_instructions
    assert "Do not author parameters" in runtime.last_instructions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_capability",
    ["merchant.offer.execute", "merchant.offer.fabricate"],
)
async def test_required_model_rejects_execute_or_invented_proposal_capability(
    bad_capability: str,
) -> None:
    runtime = _ChoiceRuntime(
        {
            "template_id": "merchant_proposal",
            "capabilities": [
                "merchant.inventory.read",
                bad_capability,
            ],
            "selected_product_ids": [7],
            "candidate_limit": 1,
        }
    )
    with provider_budget_scope(_budget_context()):
        with pytest.raises(ModelPlanRejectedError, match="model_plan_not_authorized"):
            await BoundedV2Planner(
                model_runtime=runtime,  # type: ignore[arg-type]
                runtime_mode="required",
            ).plan(
                "Đổi giá thành 200000 VND",
                _context(
                    mode=ConversationMode.MERCHANT,
                    resolved_product_ids=(7,),
                    write=True,
                ),
            )


@pytest.mark.asyncio
async def test_required_model_cannot_lower_checkout_proposal_candidate_limit() -> None:
    runtime = _ChoiceRuntime(
        {
            "template_id": "shopper_checkout_proposal",
            "capabilities": [
                "shopper.cart.read",
                "shopper.checkout.preview",
                "shopper.checkout.propose",
            ],
            "selected_product_ids": [],
            "candidate_limit": 1,
        }
    )
    with provider_budget_scope(_budget_context()):
        with pytest.raises(ModelPlanRejectedError, match="model_plan_not_authorized"):
            await BoundedV2Planner(
                model_runtime=runtime,  # type: ignore[arg-type]
                runtime_mode="required",
            ).plan(
                "Thanh toán giỏ hàng",
                _context(write=True),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "mode", "product_ids", "expected_kind"),
    [
        (
            "Thanh toán giỏ hàng",
            ConversationMode.SHOPPER,
            (),
            ActionKind.CHECKOUT,
        ),
        (
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7,),
            ActionKind.MERCHANT_PRICE_CHANGE,
        ),
        (
            "Giảm tồn kho 3",
            ConversationMode.MERCHANT,
            (7,),
            ActionKind.MERCHANT_INVENTORY_CHANGE,
        ),
    ],
)
async def test_supervisor_creates_server_owned_card_without_tools_or_grounding(
    message: str,
    mode: ConversationMode,
    product_ids: tuple[int, ...],
    expected_kind: ActionKind,
) -> None:
    context = _context(
        mode=mode,
        resolved_product_ids=product_ids,
        write=True,
    )
    action_service = _FakeActionService()
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=action_service,  # type: ignore[arg-type]
    )
    computation = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message=message,
        context=context,
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome == DialogueOutcome.AWAITING_CONFIRMATION
    assert len(computation.result.action_cards) == 1
    assert computation.result.action_cards[0].kind == expected_kind
    assert computation.result.claims == ()
    assert computation.result.citations == ()
    assert executor.calls == []
    assert producer.calls == 0
    assert action_service.proposal_lease_owners == ["worker_action"]
    if expected_kind == ActionKind.CHECKOUT:
        assert action_service.checkout_requests == [
            CheckoutInput(cart_id="cart_server_owned", expected_version=4)
        ]
    elif expected_kind == ActionKind.MERCHANT_PRICE_CHANGE:
        assert action_service.offer_requests == [
            MerchantOfferProposalInput(
                offer_id="offer_server_owned",
                expected_version=6,
                new_price_vnd=200_000,
            )
        ]
    else:
        assert action_service.offer_requests == [
            MerchantOfferProposalInput(
                offer_id="offer_server_owned",
                expected_version=6,
                quantity_delta=-3,
            )
        ]


@pytest.mark.asyncio
async def test_missing_action_service_is_safe_clarification() -> None:
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
    )
    computation = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message="Thanh toán giỏ hàng",
        context=_context(write=True),
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert computation.result.warnings == ("action_service_unavailable",)
    assert executor.calls == []
    assert producer.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "error_type", "message", "mode", "product_ids", "code"),
    [
        (
            "read_cart",
            ResourceNotFoundError,
            "Thanh toán giỏ hàng",
            ConversationMode.SHOPPER,
            (),
            "checkout_cart_unavailable",
        ),
        (
            "read_cart",
            ActionConflictError,
            "Thanh toán giỏ hàng",
            ConversationMode.SHOPPER,
            (),
            "action_prerequisite_changed",
        ),
        (
            "propose_checkout",
            ResourceNotFoundError,
            "Thanh toán giỏ hàng",
            ConversationMode.SHOPPER,
            (),
            "action_prerequisite_changed",
        ),
        (
            "propose_checkout",
            ActionConflictError,
            "Thanh toán giỏ hàng",
            ConversationMode.SHOPPER,
            (),
            "action_prerequisite_changed",
        ),
        (
            "read_inventory",
            ResourceNotFoundError,
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7,),
            "merchant_offer_unavailable",
        ),
        (
            "read_inventory",
            ActionConflictError,
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7,),
            "action_prerequisite_changed",
        ),
        (
            "propose_offer",
            ResourceNotFoundError,
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7,),
            "action_prerequisite_changed",
        ),
        (
            "propose_offer",
            ActionConflictError,
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7,),
            "action_prerequisite_changed",
        ),
    ],
)
async def test_proposal_current_state_races_return_stable_clarification(
    stage: str,
    error_type: type[Exception],
    message: str,
    mode: ConversationMode,
    product_ids: tuple[int, ...],
    code: str,
) -> None:
    action_service = _FakeActionService(fail_at=stage, error=error_type())
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=action_service,  # type: ignore[arg-type]
    )
    computation = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message=message,
        context=_context(mode=mode, resolved_product_ids=product_ids, write=True),
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert computation.result.warnings == (code,)
    assert computation.result.action_cards == ()
    assert executor.calls == []
    assert producer.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "error", "message", "mode", "product_ids", "code"),
    [
        *[
            (
                stage,
                AuthorizationDeniedError(),
                message,
                mode,
                product_ids,
                "action_write_permission_required",
            )
            for stage, message, mode, product_ids in (
                ("read_cart", "Thanh toán giỏ hàng", ConversationMode.SHOPPER, ()),
                (
                    "propose_checkout",
                    "Thanh toán giỏ hàng",
                    ConversationMode.SHOPPER,
                    (),
                ),
                (
                    "read_inventory",
                    "Đổi giá thành 200000 VND",
                    ConversationMode.MERCHANT,
                    (7,),
                ),
                (
                    "propose_offer",
                    "Đổi giá thành 200000 VND",
                    ConversationMode.MERCHANT,
                    (7,),
                ),
            )
        ],
        *[
            (
                stage,
                ActionServiceError("unavailable"),
                message,
                mode,
                product_ids,
                "action_service_unavailable",
            )
            for stage, message, mode, product_ids in (
                ("read_cart", "Thanh toán giỏ hàng", ConversationMode.SHOPPER, ()),
                (
                    "propose_checkout",
                    "Thanh toán giỏ hàng",
                    ConversationMode.SHOPPER,
                    (),
                ),
                (
                    "read_inventory",
                    "Đổi giá thành 200000 VND",
                    ConversationMode.MERCHANT,
                    (7,),
                ),
                (
                    "propose_offer",
                    "Đổi giá thành 200000 VND",
                    ConversationMode.MERCHANT,
                    (7,),
                ),
            )
        ],
    ],
)
async def test_proposal_domain_errors_return_stable_clarification(
    stage: str,
    error: Exception,
    message: str,
    mode: ConversationMode,
    product_ids: tuple[int, ...],
    code: str,
) -> None:
    action_service = _FakeActionService(fail_at=stage, error=error)
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=action_service,  # type: ignore[arg-type]
    )

    computation = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message=message,
        context=_context(mode=mode, resolved_product_ids=product_ids, write=True),
        deadline_monotonic=10**12,
    )

    assert computation.result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert computation.result.warnings == (code,)
    assert computation.result.action_cards == ()
    assert executor.calls == []
    assert producer.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "message", "mode", "product_ids"),
    [
        ("read_cart", "Thanh toán giỏ hàng", ConversationMode.SHOPPER, ()),
        ("propose_checkout", "Thanh toán giỏ hàng", ConversationMode.SHOPPER, ()),
        (
            "read_inventory",
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7,),
        ),
        (
            "propose_offer",
            "Đổi giá thành 200000 VND",
            ConversationMode.MERCHANT,
            (7,),
        ),
    ],
)
async def test_proposal_service_programming_errors_remain_visible(
    stage: str,
    message: str,
    mode: ConversationMode,
    product_ids: tuple[int, ...],
) -> None:
    action_service = _FakeActionService(
        fail_at=stage,
        error=RuntimeError("unexpected service failure"),
    )
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=action_service,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="unexpected service failure"):
        await supervisor.run_claimed(
            conversation_id="conversation_action",
            turn_id="turn_action",
            lease_owner="worker_action",
            message=message,
            context=_context(mode=mode, resolved_product_ids=product_ids, write=True),
            deadline_monotonic=10**12,
        )
    assert executor.calls == []
    assert producer.calls == 0


@pytest.mark.asyncio
async def test_ambiguous_action_price_uses_exact_price_clarification() -> None:
    action_service = _FakeActionService()
    executor = _FakeOperationExecutor()
    producer = _AnswerProducer()
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=BoundedV2Planner(runtime_mode="off"),
        operation_executor=executor,  # type: ignore[arg-type]
        answer_producer=producer,
        action_service=action_service,  # type: ignore[arg-type]
    )
    computation = await supervisor.run_claimed(
        conversation_id="conversation_action",
        turn_id="turn_action",
        lease_owner="worker_action",
        message="Đổi giá thành 1,5 VND",
        context=_context(
            mode=ConversationMode.MERCHANT,
            resolved_product_ids=(7,),
            write=True,
        ),
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome == DialogueOutcome.NEEDS_CLARIFICATION
    assert computation.result.warnings == ("merchant_price_value_ambiguous",)
    assert computation.result.answer == (
        "Bạn hãy nêu chính xác mức giá mới bằng số VND nguyên."
    )
    assert executor.calls == []
    assert producer.calls == 0
    assert action_service.read_inventory_calls == 0
    assert action_service.offer_requests == []


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
    assert runtime.last_max_output_tokens == GENERATION_OUTPUT_TOKEN_LIMIT


def test_expert_payload_preflight_accounts_for_schema_and_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedExpertSelection(BaseModel):
        padding: str = Field(default="", description="x" * 12_000)

    operation = __import__("asyncio").run(
        BoundedV2Planner(runtime_mode="off").plan(
            "Tìm sách Sapiens", _context(resolved_product_ids=(1,))
        )
    ).initial_operations[0]
    deterministic = _product_result(operation, (1,))
    monkeypatch.setattr(
        "app.v2.execution.ExpertEvidenceSelection", OversizedExpertSelection
    )
    input_text, fact_ids, evidence_ids = _expert_model_input(deterministic)

    assert json.loads(input_text)["fact_catalog"] == []
    assert fact_ids == frozenset()
    assert evidence_ids == frozenset()


def test_expert_payload_preflight_accounts_for_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = __import__("asyncio").run(
        BoundedV2Planner(runtime_mode="off").plan(
            "Tìm sách Sapiens", _context(resolved_product_ids=(1,))
        )
    ).initial_operations[0]
    deterministic = _product_result(operation, (1,))
    monkeypatch.setattr("app.v2.execution._EXPERT_MODEL_INSTRUCTIONS", "x" * 12_000)

    input_text, fact_ids, evidence_ids = _expert_model_input(deterministic)

    assert json.loads(input_text)["fact_catalog"] == []
    assert fact_ids == frozenset()
    assert evidence_ids == frozenset()


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
        conversation_id="conversation_recommendation",
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
        conversation_id="conversation_staged_mixed",
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
        conversation_id="conversation_empty",
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
        conversation_id="conversation_missing_trust",
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
        conversation_id="conversation_knowledge",
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


def _action_card(
    kind: ActionKind,
    *,
    target_type: Literal["cart", "offer"],
    target_id: str,
    target_version: int,
) -> ActionCard:
    if kind == ActionKind.CHECKOUT:
        change = ActionChange(
            resource_type="order",
            resource_id="order_test",
            field="status",
            before_text="cart_active",
            after_text="confirmed",
        )
    elif kind == ActionKind.MERCHANT_PRICE_CHANGE:
        change = ActionChange(
            resource_type="offer",
            resource_id=target_id,
            field="price_vnd",
            before_integer=100_000,
            after_integer=200_000,
        )
    else:
        change = ActionChange(
            resource_type="offer",
            resource_id=target_id,
            field="quantity",
            before_integer=8,
            after_integer=5,
        )
    return ActionCard(
        action_id=f"proposal_{kind.value}_test",
        proposal_id=f"proposal_{kind.value}_test",
        proposal_version=1,
        kind=kind,
        status=ActionStatus.PROPOSED,
        title="Xác nhận thay đổi sandbox",
        required_permission=(
            "ecommerce.write" if kind == ActionKind.CHECKOUT else "merchant.write"
        ),
        confirmation_required=True,
        target=ActionTarget(
            resource_type=target_type,
            resource_id=target_id,
            expected_resource_version=target_version,
            data_version_ids=("catalog_v1", "sandbox_test"),
        ),
        changes=(change,),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _context(
    *,
    mode: ConversationMode = ConversationMode.SHOPPER,
    resolved_product_ids: tuple[int, ...] = (),
    write: bool = False,
) -> PlanningContext:
    scopes = {"ecommerce.read"}
    if mode == ConversationMode.MERCHANT:
        scopes.add("merchant.read")
        if write:
            scopes.add("merchant.write")
    elif write:
        scopes.add("ecommerce.write")
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


def _context_with_scopes(
    mode: ConversationMode,
    scopes: frozenset[str],
    product_ids: tuple[int, ...],
) -> PlanningContext:
    return PlanningContext(
        access=ResourceAuthorization(
            binding=ResourceBinding(
                tenant_id="tenant_test",
                principal_id="principal_test",
                mode=mode,
                store_id="demo",
            ),
            scopes=scopes,
        ),
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_v1",
            corpus_version_id="corpus_v1",
            index_manifest_id="index_v1",
        ),
        resolved_product_ids=product_ids,
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
