"""One bounded supervisor for shopper and merchant v2 read turns."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Protocol

from sqlalchemy.orm import Session

from app.contracts import AuthorizationContext, TaskStatus
from app.knowledge.v2_contracts import ResolvedKnowledgeEvidence
from app.models.budget import ProviderBudgetScope
from app.shared.budget import (
    DEFAULT_MAX_GENERATION_CALLS,
    DEFAULT_MAX_PROVIDER_ATTEMPTS,
    DEFAULT_TURN_LIMIT_NANO_USD,
    BudgetCancelledError,
    current_provider_budget,
    nano_usd_to_usd,
)
from app.v2.actions import (
    ActionConflictError,
    ActionServiceError,
    StoredAction,
    V2ActionService,
)
from app.v2.answers import EvidenceRequirement
from app.v2.authorization import (
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceNotFoundError,
)
from app.v2.contracts import (
    MAX_DRAFT_REPAIRS,
    MAX_EXPERT_STEPS,
    MAX_KNOWLEDGE_RETRIEVALS,
    DialogueOutcome,
    EvidenceKind,
    EvidenceReference,
    ExecutionRecord,
    PlanRevision,
    PlanStep,
    PlanTrace,
    TurnResult,
)
from app.v2.execution import (
    DurableOperationExecutor,
    OperationBatch,
    TurnComputation,
)
from app.v2.planning import BoundedV2Planner, PlannedTurn, PlanningContext
from app.v2.registry import (
    CartReadInput,
    CheckoutInput,
    MerchantOfferProposalInput,
    MerchantReadInput,
    MerchantReadResult,
)
from app.v2.runtime_contracts import (
    EvidenceAssessment,
    EvidenceExcerpt,
    EvidenceObligation,
    EvidenceObligationKind,
    ExpertResult,
    GroundingResult,
    RuntimeOperation,
    StructuredFact,
    ToolEvidence,
)

SessionFactory = Callable[[], Session]
BoundKnowledgeResolver = Callable[
    [EvidenceReference], Awaitable[ResolvedKnowledgeEvidence]
]
KnowledgeResolver = Callable[
    [EvidenceReference, ResourceAuthorization],
    Awaitable[ResolvedKnowledgeEvidence],
]


class GroundedAnswerProducer(Protocol):
    async def produce(
        self,
        *,
        user_request: str,
        evidence: ToolEvidence,
        allowed_subject_ids: frozenset[str],
        resolve_knowledge: BoundKnowledgeResolver | None = None,
        allow_repair: bool | Callable[[], bool] = False,
        requirements: tuple[EvidenceRequirement, ...] = (),
    ) -> GroundingResult: ...


@dataclass(frozen=True, slots=True)
class _BudgetRemaining:
    seconds: float
    generation_calls: int
    provider_attempts: int
    cost_usd: Decimal


class V2ReadSupervisor:
    """Plan, dispatch, assess, continue once, and publish grounded read results."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        planner: BoundedV2Planner,
        operation_executor: DurableOperationExecutor,
        answer_producer: GroundedAnswerProducer,
        knowledge_resolver: KnowledgeResolver | None = None,
        action_service: V2ActionService | None = None,
        continuation_enabled: bool = True,
    ) -> None:
        if type(continuation_enabled) is not bool:
            raise TypeError("continuation_enabled must be a bool")
        self.session_factory = session_factory
        self.planner = planner
        self.operation_executor = operation_executor
        self.answer_producer = answer_producer
        self.knowledge_resolver = knowledge_resolver
        self.action_service = action_service
        self.continuation_enabled = continuation_enabled

    def read_turn_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        access: ResourceAuthorization,
    ) -> StoredAction | None:
        if self.action_service is None:
            return None
        binding = access.binding
        return self.action_service.read_turn_proposal(
            AuthorizationContext(
                tenant_id=binding.tenant_id,
                principal_id=binding.principal_id,
                scopes=access.scopes,
            ),
            conversation_id=conversation_id,
            turn_id=turn_id,
            mode=binding.mode,
        )

    def recover_expired_turn_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        access: ResourceAuthorization,
    ) -> bool:
        if self.action_service is None:
            return False
        binding = access.binding
        return self.action_service.recover_expired_turn_proposal(
            AuthorizationContext(
                tenant_id=binding.tenant_id,
                principal_id=binding.principal_id,
                scopes=access.scopes,
            ),
            conversation_id=conversation_id,
            turn_id=turn_id,
            mode=binding.mode,
        )

    async def run_claimed(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        lease_owner: str,
        message: str,
        context: PlanningContext,
        deadline_monotonic: float,
    ) -> TurnComputation:
        planned = await self.planner.plan(message, context)
        fallback_reasons = (
            (planned.fallback_reason,) if planned.fallback_reason is not None else ()
        )
        if planned.clarification_code is not None:
            result = TurnResult(
                outcome=DialogueOutcome.NEEDS_CLARIFICATION,
                answer=_clarification_answer(planned.clarification_code),
                plan=_plan_trace(planned, (), None, None),
                warnings=(planned.clarification_code,),
            )
            return TurnComputation(
                result=result,
                fallback_reasons=fallback_reasons,
            )

        if planned.proposal is not None:
            if planned.initial_operations:
                return await self._read_and_create_proposal(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    lease_owner=lease_owner,
                    message=message,
                    planned=planned,
                    context=context,
                    fallback_reasons=fallback_reasons,
                )
            return self._create_proposal(
                conversation_id=conversation_id,
                turn_id=turn_id,
                lease_owner=lease_owner,
                planned=planned,
                context=context,
                fallback_reasons=fallback_reasons,
            )

        initial = await self.operation_executor.execute(
            turn_id=turn_id,
            lease_owner=lease_owner,
            access=context.access,
            operations=planned.initial_operations,
            plan_revision=0,
        )
        all_operations = planned.initial_operations
        initial_candidates = _candidate_product_ids(initial.results)
        bound_initial_operations = self.planner.bind_initial_candidates(
            planned,
            initial_candidates,
            context,
            all_operations,
        )
        if bound_initial_operations:
            bound_initial = await self.operation_executor.execute(
                turn_id=turn_id,
                lease_owner=lease_owner,
                access=context.access,
                operations=bound_initial_operations,
                plan_revision=0,
            )
            all_operations = (*all_operations, *bound_initial_operations)
            initial = OperationBatch(
                results=(*initial.results, *bound_initial.results),
                dispatched_step_ids=(
                    *initial.dispatched_step_ids,
                    *bound_initial.dispatched_step_ids,
                ),
                reused_step_ids=(
                    *initial.reused_step_ids,
                    *bound_initial.reused_step_ids,
                ),
            )
            planned = replace(
                planned,
                initial_operations=all_operations,
                deferred_capabilities=tuple(
                    capability
                    for capability in planned.deferred_capabilities
                    if capability
                    not in {
                        operation.capability for operation in bound_initial_operations
                    }
                ),
            )
        all_results = initial.results
        reused_step_ids = set(initial.reused_step_ids)
        budget = self._budget_remaining(turn_id, deadline_monotonic)
        assessment = assess_evidence(
            planned.obligations,
            all_results,
            remaining=budget,
            plan_revisions_used=0,
            added_reads_used=0,
            draft_repairs_used=0,
        )

        continuation_operations = (
            self.planner.continue_plan(
                planned,
                assessment,
                context,
                all_operations,
            )
            if self.continuation_enabled
            else ()
        )
        continuation: OperationBatch | None = None
        if continuation_operations:
            continuation = await self.operation_executor.execute(
                turn_id=turn_id,
                lease_owner=lease_owner,
                access=context.access,
                operations=continuation_operations,
                plan_revision=1,
            )
            all_operations = (*all_operations, *continuation_operations)
            all_results = (*all_results, *continuation.results)
            reused_step_ids.update(continuation.reused_step_ids)
            budget = self._budget_remaining(turn_id, deadline_monotonic)
            assessment = assess_evidence(
                planned.obligations,
                all_results,
                remaining=budget,
                plan_revisions_used=1,
                added_reads_used=len(continuation_operations),
                draft_repairs_used=0,
            )

        plan_trace = _plan_trace(
            planned,
            all_operations,
            initial,
            continuation,
        )
        execution_records = _execution_records(
            all_results,
            reused_step_ids,
        )
        evidence = merge_tool_evidence(all_results)
        fallback_reasons = tuple(
            dict.fromkeys(
                (
                    *fallback_reasons,
                    *(
                        result.reasoning_fallback_reason
                        for result in all_results
                        if result.reasoning_fallback_reason is not None
                    ),
                )
            )
        )
        warnings = tuple(
            dict.fromkeys(
                (
                    *(f"model_fallback:{reason}" for reason in fallback_reasons),
                    *(f"tool_error:{error.code}" for error in assessment.errors),
                )
            )
        )

        if assessment.conflicting_obligation_ids:
            result = TurnResult(
                outcome=DialogueOutcome.ABSTAINED,
                answer=(
                    "Mình chưa thể trả lời vì các nguồn đã kiểm tra đang mâu thuẫn."
                ),
                plan=plan_trace,
                executions=execution_records,
                warnings=(*warnings, "evidence_conflict"),
            )
            return TurnComputation(
                result=result,
                fallback_reasons=fallback_reasons,
                knowledge_retrievals=assessment.knowledge_retrievals_used,
            )
        if assessment.missing_obligation_ids:
            outcome = (
                DialogueOutcome.NEEDS_CLARIFICATION
                if _needs_entity_clarification(planned, assessment)
                else DialogueOutcome.ABSTAINED
            )
            answer = (
                "Mình chưa tìm thấy đủ ứng viên đã xác minh; bạn hãy nêu rõ hơn "
                "tên sách hoặc tác giả."
                if outcome == DialogueOutcome.NEEDS_CLARIFICATION
                else "Mình chưa có đủ bằng chứng đã xác minh để trả lời yêu cầu này."
            )
            result = TurnResult(
                outcome=outcome,
                answer=answer,
                plan=plan_trace,
                executions=execution_records,
                warnings=(*warnings, "evidence_incomplete"),
            )
            return TurnComputation(
                result=result,
                fallback_reasons=fallback_reasons,
                knowledge_retrievals=assessment.knowledge_retrievals_used,
            )

        allowed_subject_ids = frozenset(
            f"product_{product_id}"
            for product_id in (
                *context.resolved_product_ids,
                *assessment.candidate_product_ids,
            )
        )
        resolver = self._bound_resolver(context.access)
        requirements = _grounding_requirements(planned, assessment)
        grounding_evidence = select_grounding_evidence(
            all_results,
            planned.obligations,
            merged=evidence,
        )
        repair_checked = False
        repair_authorized = False

        def allow_repair() -> bool:
            nonlocal repair_checked, repair_authorized
            if repair_checked:
                raise ValueError("draft repair authorization checked more than once")
            repair_checked = True
            budget_context = current_provider_budget()
            if budget_context is None:
                return False
            if budget_context.cancelled():
                raise BudgetCancelledError("provider_dispatch_cancelled")
            remaining = self._budget_remaining(turn_id, deadline_monotonic)
            repair_authorized = bool(
                assessment.draft_repairs_used < MAX_DRAFT_REPAIRS
                and remaining.generation_calls >= 2
                and remaining.provider_attempts >= 2
                and remaining.cost_usd > 0
                and remaining.seconds > 0
            )
            if repair_authorized:
                self.operation_executor.checkpoint_draft_repair(
                    turn_id=turn_id,
                    lease_owner=lease_owner,
                    access=context.access,
                )
            return repair_authorized

        grounded = await self.answer_producer.produce(
            user_request=message,
            evidence=grounding_evidence,
            allowed_subject_ids=allowed_subject_ids,
            resolve_knowledge=resolver,
            allow_repair=allow_repair,
            requirements=requirements,
        )
        if grounded.draft_repairs > 0 and not repair_authorized:
            raise ValueError("grounding exceeded the allowed draft repair count")
        result = TurnResult(
            outcome=grounded.outcome,
            answer=grounded.answer,
            claims=grounded.claims,
            citations=grounded.citations,
            evidence=grounded.evidence,
            plan=plan_trace,
            executions=execution_records,
            warnings=tuple(dict.fromkeys((*warnings, *grounded.warnings))),
        )
        return TurnComputation(
            result=result,
            fallback_reasons=fallback_reasons,
            knowledge_retrievals=assessment.knowledge_retrievals_used,
            draft_repairs=grounded.draft_repairs,
        )

    async def _read_and_create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        lease_owner: str,
        message: str,
        planned: PlannedTurn,
        context: PlanningContext,
        fallback_reasons: tuple[str, ...],
    ) -> TurnComputation:
        batch = await self.operation_executor.execute(
            turn_id=turn_id,
            lease_owner=lease_owner,
            access=context.access,
            operations=planned.initial_operations,
            plan_revision=0,
        )
        trace = _plan_trace(planned, planned.initial_operations, batch, None)
        records = _execution_records(batch.results, set(batch.reused_step_ids))
        if (
            len(batch.results) != 1
            or batch.results[0].status != TaskStatus.SUCCESS
            or batch.results[0].output is None
        ):
            return TurnComputation(
                result=TurnResult(
                    outcome=DialogueOutcome.ABSTAINED,
                    answer="Mình chưa có đủ bằng chứng đã xác minh để tạo đề xuất.",
                    plan=trace,
                    executions=records,
                    warnings=("merchant_inventory_evidence_incomplete",),
                ),
                fallback_reasons=fallback_reasons,
            )
        inventory = MerchantReadResult.model_validate(batch.results[0].output)
        products = tuple(offer.product_id for offer in inventory.offers)
        if len(products) != len(set(products)) or set(products) != set(
            context.resolved_product_ids
        ):
            return TurnComputation(
                result=TurnResult(
                    outcome=DialogueOutcome.NEEDS_CLARIFICATION,
                    answer="Mình chưa xác minh được đủ các offer cần đọc.",
                    plan=trace,
                    executions=records,
                    warnings=("merchant_inventory_products_mismatch",),
                ),
                fallback_reasons=fallback_reasons,
            )
        evidence = merge_tool_evidence(batch.results)
        if any(
            not any(
                fact.subject_id == f"offer_{offer.offer_id}"
                and fact.field == "demo_price_vnd"
                and fact.value == offer.price_vnd
                for fact in evidence.facts
            )
            for offer in inventory.offers
        ):
            return TurnComputation(
                result=TurnResult(
                    outcome=DialogueOutcome.ABSTAINED,
                    answer="Mình chưa có đủ giá đã xác minh để tạo đề xuất.",
                    plan=trace,
                    executions=records,
                    warnings=("merchant_inventory_evidence_incomplete",),
                ),
                fallback_reasons=fallback_reasons,
            )
        fallback_reasons = tuple(
            dict.fromkeys(
                (
                    *fallback_reasons,
                    *(
                        result.reasoning_fallback_reason
                        for result in batch.results
                        if result.reasoning_fallback_reason is not None
                    ),
                )
            )
        )
        subjects = frozenset(f"offer_{offer.offer_id}" for offer in inventory.offers)
        grounded = await self.answer_producer.produce(
            user_request=message,
            evidence=evidence,
            allowed_subject_ids=subjects,
            requirements=tuple(
                EvidenceRequirement(
                    kind=EvidenceKind.SANDBOX,
                    subject_id=subject,
                    field="demo_price_vnd",
                )
                for subject in sorted(subjects)
            ),
            allow_repair=False,
        )
        read_result = TurnResult(
            outcome=grounded.outcome,
            answer=grounded.answer,
            claims=grounded.claims,
            citations=grounded.citations,
            evidence=grounded.evidence,
            plan=trace,
            executions=records,
            warnings=grounded.warnings,
        )
        if grounded.outcome != DialogueOutcome.ANSWERED:
            return TurnComputation(
                result=read_result, fallback_reasons=fallback_reasons
            )
        proposal = self._create_proposal(
            conversation_id=conversation_id,
            turn_id=turn_id,
            lease_owner=lease_owner,
            planned=planned,
            context=context,
            fallback_reasons=fallback_reasons,
            inventory=inventory,
        )
        result = TurnResult.model_validate(
            {
                **read_result.model_dump(mode="python"),
                "outcome": proposal.result.outcome,
                "answer": grounded.answer + "\n\n" + proposal.result.answer,
                "action_cards": proposal.result.action_cards,
                "warnings": (*grounded.warnings, *proposal.result.warnings),
            }
        )
        return TurnComputation(result=result, fallback_reasons=fallback_reasons)

    def _create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        lease_owner: str,
        planned: PlannedTurn,
        context: PlanningContext,
        fallback_reasons: tuple[str, ...],
        inventory: MerchantReadResult | None = None,
    ) -> TurnComputation:
        proposal = planned.proposal
        assert proposal is not None
        if self.action_service is None:
            return _proposal_clarification(
                planned,
                "action_service_unavailable",
                fallback_reasons,
            )
        authorization = AuthorizationContext(
            tenant_id=context.access.binding.tenant_id,
            principal_id=context.access.binding.principal_id,
            scopes=context.access.scopes,
        )
        if proposal.kind == "checkout":
            try:
                cart = self.action_service.read_cart(
                    authorization,
                    CartReadInput(),
                )
            except ResourceNotFoundError:
                return _proposal_clarification(
                    planned,
                    "checkout_cart_unavailable",
                    fallback_reasons,
                )
            except ActionConflictError:
                return _proposal_clarification(
                    planned,
                    "action_prerequisite_changed",
                    fallback_reasons,
                )
            except AuthorizationDeniedError:
                return _proposal_clarification(
                    planned,
                    "action_write_permission_required",
                    fallback_reasons,
                )
            except ActionServiceError:
                return _proposal_clarification(
                    planned,
                    "action_service_unavailable",
                    fallback_reasons,
                )
            checkout_request = CheckoutInput(
                cart_id=cart.cart_id,
                expected_version=cart.version,
            )
            try:
                result = self.action_service.propose_checkout(
                    authorization,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    lease_owner=lease_owner,
                    request=checkout_request,
                )
            except (ResourceNotFoundError, ActionConflictError):
                return _proposal_clarification(
                    planned,
                    "action_prerequisite_changed",
                    fallback_reasons,
                )
            except AuthorizationDeniedError:
                return _proposal_clarification(
                    planned,
                    "action_write_permission_required",
                    fallback_reasons,
                )
            except ActionServiceError:
                return _proposal_clarification(
                    planned,
                    "action_service_unavailable",
                    fallback_reasons,
                )
        else:
            assert proposal.product_id is not None
            try:
                if inventory is None:
                    inventory = self.action_service.read_inventory(
                        authorization,
                        MerchantReadInput(product_ids=(proposal.product_id,)),
                    )
            except ResourceNotFoundError:
                return _proposal_clarification(
                    planned,
                    "merchant_offer_unavailable",
                    fallback_reasons,
                )
            except ActionConflictError:
                return _proposal_clarification(
                    planned,
                    "action_prerequisite_changed",
                    fallback_reasons,
                )
            except AuthorizationDeniedError:
                return _proposal_clarification(
                    planned,
                    "action_write_permission_required",
                    fallback_reasons,
                )
            except ActionServiceError:
                return _proposal_clarification(
                    planned,
                    "action_service_unavailable",
                    fallback_reasons,
                )
            offers = tuple(
                offer
                for offer in inventory.offers
                if offer.product_id == proposal.product_id
            )
            if len(offers) != 1:
                return _proposal_clarification(
                    planned,
                    "merchant_offer_unavailable",
                    fallback_reasons,
                )
            offer = offers[0]
            target = context.merchant_target
            if target is not None and (
                offer.offer_id != target.offer_id
                or offer.product_id != target.product_id
                or offer.version != target.expected_version
            ):
                return _proposal_clarification(
                    planned,
                    "action_prerequisite_changed",
                    fallback_reasons,
                )
            offer_request = MerchantOfferProposalInput(
                offer_id=offer.offer_id,
                expected_version=offer.version,
                new_price_vnd=proposal.new_price_vnd,
                quantity_delta=proposal.quantity_delta,
            )
            try:
                result = self.action_service.propose_offer_change(
                    authorization,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    lease_owner=lease_owner,
                    request=offer_request,
                )
            except (ResourceNotFoundError, ActionConflictError):
                return _proposal_clarification(
                    planned,
                    "action_prerequisite_changed",
                    fallback_reasons,
                )
            except AuthorizationDeniedError:
                return _proposal_clarification(
                    planned,
                    "action_write_permission_required",
                    fallback_reasons,
                )
            except ActionServiceError:
                return _proposal_clarification(
                    planned,
                    "action_service_unavailable",
                    fallback_reasons,
                )
        return TurnComputation(
            result=TurnResult(
                outcome=DialogueOutcome.AWAITING_CONFIRMATION,
                answer=(
                    "Vui lòng kiểm tra thẻ xác nhận bên dưới và dùng nút hành động "
                    "để xác nhận hoặc từ chối."
                ),
                action_cards=(result.action,),
                plan=_plan_trace(planned, (), None, None),
            ),
            fallback_reasons=fallback_reasons,
        )

    def _bound_resolver(
        self,
        access: ResourceAuthorization,
    ) -> BoundKnowledgeResolver | None:
        if self.knowledge_resolver is None:
            return None

        async def resolve(
            reference: EvidenceReference,
        ) -> ResolvedKnowledgeEvidence:
            assert self.knowledge_resolver is not None
            return await self.knowledge_resolver(reference, access)

        return resolve

    def _budget_remaining(
        self,
        turn_id: str,
        deadline_monotonic: float,
    ) -> _BudgetRemaining:
        remaining_seconds = max(
            0.0,
            min(60.0, deadline_monotonic - monotonic()),
        )
        budget = current_provider_budget()
        if budget is None:
            return _BudgetRemaining(
                seconds=remaining_seconds,
                generation_calls=DEFAULT_MAX_GENERATION_CALLS,
                provider_attempts=DEFAULT_MAX_PROVIDER_ATTEMPTS,
                cost_usd=nano_usd_to_usd(DEFAULT_TURN_LIMIT_NANO_USD),
            )
        if budget.scope_id != turn_id:
            raise ValueError("provider budget scope does not match the turn")
        with self.session_factory() as session:
            scope = session.get(ProviderBudgetScope, turn_id)
            if scope is None:
                raise ValueError("provider budget scope is missing")
            generation_calls = scope.generation_calls
            max_generation_calls = scope.max_generation_calls
            provider_attempts = scope.provider_attempts
            max_provider_attempts = scope.max_provider_attempts
            encumbered = (
                scope.known_cost_nano_usd
                + scope.reserved_cost_nano_usd
                + scope.unknown_cost_nano_usd
            )
            hard_limit = scope.hard_limit_nano_usd
            deadline_at = _aware(scope.deadline_at)
        if deadline_at is not None:
            remaining_seconds = min(
                remaining_seconds,
                max(0.0, (deadline_at - datetime.now(UTC)).total_seconds()),
            )
        return _BudgetRemaining(
            seconds=remaining_seconds,
            generation_calls=max(0, max_generation_calls - generation_calls),
            provider_attempts=max(0, max_provider_attempts - provider_attempts),
            cost_usd=nano_usd_to_usd(max(0, hard_limit - encumbered)),
        )


def assess_evidence(
    obligations: tuple[EvidenceObligation, ...],
    results: tuple[ExpertResult, ...],
    *,
    remaining: _BudgetRemaining,
    plan_revisions_used: int,
    added_reads_used: int,
    draft_repairs_used: int,
) -> EvidenceAssessment:
    """Build the evidence state from validated tool results and SQL budget state."""

    unique_results = {result.operation.operation_key: result for result in results}
    if len(unique_results) > MAX_EXPERT_STEPS:
        raise ValueError("expert result count exceeds the turn limit")
    candidates = _candidate_product_ids(tuple(unique_results.values()))
    conflicting = _conflicting_obligations(tuple(unique_results.values()))
    fulfilled: list[str] = []
    missing: list[str] = []
    for obligation in obligations:
        if obligation.obligation_id in conflicting:
            continue
        if _obligation_satisfied(
            obligation,
            tuple(unique_results.values()),
            candidates,
        ):
            fulfilled.append(obligation.obligation_id)
        else:
            missing.append(obligation.obligation_id)
    errors = tuple(
        result.error for result in unique_results.values() if result.error is not None
    )
    retryable_keys = tuple(
        result.operation.operation_key
        for result in unique_results.values()
        if result.error is not None and result.error.retryable
    )
    knowledge_count = sum(
        result.operation.capability == "knowledge.retrieve"
        for result in unique_results.values()
    )
    if knowledge_count > MAX_KNOWLEDGE_RETRIEVALS:
        raise ValueError("knowledge retrieval count exceeds the turn limit")
    return EvidenceAssessment(
        fulfilled_obligation_ids=tuple(fulfilled),
        missing_obligation_ids=tuple(missing),
        conflicting_obligation_ids=tuple(
            obligation.obligation_id
            for obligation in obligations
            if obligation.obligation_id in conflicting
        ),
        candidate_product_ids=candidates,
        errors=errors,
        retryable_operation_keys=retryable_keys,
        remaining_seconds=remaining.seconds,
        remaining_generation_calls=remaining.generation_calls,
        remaining_provider_attempts=remaining.provider_attempts,
        remaining_cost_usd=remaining.cost_usd,
        plan_revisions_used=plan_revisions_used,
        expert_steps_used=len(unique_results),
        added_reads_used=added_reads_used,
        knowledge_retrievals_used=knowledge_count,
        draft_repairs_used=draft_repairs_used,
    )


def merge_tool_evidence(results: tuple[ExpertResult, ...]) -> ToolEvidence:
    """Merge exact evidence without silently accepting conflicting duplicate IDs."""

    facts: dict[str, StructuredFact] = {}
    excerpts: dict[str, EvidenceExcerpt] = {}
    references: dict[str, EvidenceReference] = {}
    for result in results:
        for fact in result.evidence.facts:
            fact_previous = facts.setdefault(fact.fact_id, fact)
            if fact_previous != fact:
                raise ValueError("duplicate fact ID has conflicting data")
        for excerpt in result.evidence.excerpts:
            excerpt_previous = excerpts.setdefault(excerpt.evidence_id, excerpt)
            if excerpt_previous != excerpt:
                raise ValueError("duplicate excerpt ID has conflicting data")
        for reference in result.evidence.references:
            normalized = reference.model_copy(update={"display_label": "[C1]"})
            reference_previous = references.setdefault(
                reference.evidence_id,
                normalized,
            )
            if reference_previous != normalized:
                raise ValueError("duplicate evidence ID has conflicting metadata")
    relabelled = tuple(
        reference.model_copy(update={"display_label": f"[C{index}]"})
        for index, reference in enumerate(references.values(), start=1)
    )
    return ToolEvidence(
        facts=tuple(facts.values()),
        excerpts=tuple(excerpts.values()),
        references=relabelled,
    )


def select_grounding_evidence(
    results: tuple[ExpertResult, ...],
    obligations: tuple[EvidenceObligation, ...],
    *,
    merged: ToolEvidence | None = None,
) -> ToolEvidence:
    """Apply valid expert selectors while restoring explicit obligations."""

    full = merged if merged is not None else merge_tool_evidence(results)
    if not any(
        result.selected_fact_ids or result.selected_evidence_ids for result in results
    ):
        return full
    explicit_ids = {
        obligation.obligation_id for obligation in obligations if obligation.explicit
    }
    keep_fact_ids: set[str] = set()
    keep_evidence_ids: set[str] = set()
    for result in results:
        restore = bool(set(result.operation.obligation_ids) & explicit_ids)
        deterministic_fallback = result.reasoning_fallback_reason is not None
        if (
            restore
            or deterministic_fallback
            or (not result.selected_fact_ids and not result.selected_evidence_ids)
        ):
            keep_fact_ids.update(fact.fact_id for fact in result.evidence.facts)
            keep_evidence_ids.update(
                reference.evidence_id for reference in result.evidence.references
            )
            continue
        keep_fact_ids.update(result.selected_fact_ids)
        keep_evidence_ids.update(result.selected_evidence_ids)

    facts = tuple(fact for fact in full.facts if fact.fact_id in keep_fact_ids)
    keep_evidence_ids.update(
        evidence_id for fact in facts for evidence_id in fact.evidence_ids
    )
    return ToolEvidence(
        facts=facts,
        excerpts=tuple(
            excerpt
            for excerpt in full.excerpts
            if excerpt.evidence_id in keep_evidence_ids
        ),
        references=tuple(
            reference
            for reference in full.references
            if reference.evidence_id in keep_evidence_ids
        ),
    )


def _grounding_requirements(
    planned: PlannedTurn,
    assessment: EvidenceAssessment,
) -> tuple[EvidenceRequirement, ...]:
    fulfilled = set(assessment.fulfilled_obligation_ids)
    candidates = assessment.candidate_product_ids
    mapping = {
        EvidenceObligationKind.CATALOG: EvidenceKind.CATALOG,
        EvidenceObligationKind.COMPARISON: EvidenceKind.CATALOG,
        EvidenceObligationKind.PRICE: EvidenceKind.CATALOG,
        EvidenceObligationKind.REVIEW: EvidenceKind.REVIEW,
        EvidenceObligationKind.TRUST: EvidenceKind.TRUST,
        EvidenceObligationKind.KNOWLEDGE: EvidenceKind.KNOWLEDGE,
        EvidenceObligationKind.MARKET: EvidenceKind.MARKET,
        EvidenceObligationKind.INVENTORY: EvidenceKind.SANDBOX,
    }
    requirements: list[EvidenceRequirement] = []
    for obligation in planned.obligations:
        if not obligation.explicit or obligation.obligation_id not in fulfilled:
            continue
        kind = mapping[obligation.kind]
        field = (
            "snapshot_price_vnd"
            if obligation.kind == EvidenceObligationKind.PRICE
            else None
        )
        subjects: tuple[str | None, ...]
        if obligation.kind == EvidenceObligationKind.MARKET:
            subjects = (None,)
        else:
            product_ids = obligation.product_ids or candidates
            subjects = (
                tuple(f"product_{product_id}" for product_id in product_ids)
                if product_ids
                else (None,)
            )
        requirements.extend(
            EvidenceRequirement(kind=kind, subject_id=subject, field=field)
            for subject in subjects
        )
    return tuple(dict.fromkeys(requirements))


def _candidate_product_ids(results: tuple[ExpertResult, ...]) -> tuple[int, ...]:
    candidates: list[int] = []
    for result in results:
        output = result.output or {}
        for key in ("products", "findings", "offers"):
            values = output.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                product_id = item.get("product_id")
                if (
                    isinstance(product_id, int)
                    and not isinstance(product_id, bool)
                    and product_id > 0
                    and product_id not in candidates
                ):
                    candidates.append(product_id)
                    if len(candidates) == 5:
                        return tuple(candidates)
    return tuple(candidates)


def _obligation_satisfied(
    obligation: EvidenceObligation,
    results: tuple[ExpertResult, ...],
    candidates: tuple[int, ...],
) -> bool:
    usable = tuple(result for result in results if result.status != TaskStatus.FAILED)
    if obligation.kind == EvidenceObligationKind.CATALOG:
        return bool(candidates) and any(
            result.operation.capability
            in {"product.catalog.search", "product.rank", "merchant.catalog.read"}
            for result in usable
        )
    if obligation.kind == EvidenceObligationKind.COMPARISON:
        return len(candidates) >= 2 and any(
            result.operation.capability in {"product.compare", "product.rank"}
            and _output_count(result, "products") >= 2
            for result in usable
        )
    if obligation.kind == EvidenceObligationKind.PRICE:
        return any(
            "price" in fact.field for result in usable for fact in result.evidence.facts
        )
    if obligation.kind == EvidenceObligationKind.REVIEW:
        return any(
            result.operation.capability in {"review.retrieve", "review.compare"}
            and _output_count(result, "findings") >= max(1, len(candidates))
            for result in usable
        )
    if obligation.kind == EvidenceObligationKind.TRUST:
        return any(
            result.operation.capability in {"trust.analyze", "trust.compare"}
            and _output_count(result, "findings") >= max(1, len(candidates))
            for result in usable
        )
    if obligation.kind == EvidenceObligationKind.KNOWLEDGE:
        return any(
            result.operation.capability == "knowledge.retrieve"
            and result.output is not None
            and result.output.get("answerable") is True
            and bool(result.evidence.excerpts)
            for result in usable
        )
    if obligation.kind == EvidenceObligationKind.MARKET:
        return any(
            result.operation.capability == "market.snapshot"
            and result.output is not None
            and "snapshot_version_id" in result.output
            for result in usable
        )
    if obligation.kind == EvidenceObligationKind.INVENTORY:
        return any(
            result.operation.capability == "merchant.inventory.read"
            and result.output is not None
            and "offers" in result.output
            for result in usable
        )
    return False


def _conflicting_obligations(results: tuple[ExpertResult, ...]) -> set[str]:
    values: dict[tuple[str, str], tuple[object, str | None]] = {}
    conflicting: set[str] = set()
    owners: dict[tuple[str, str], set[str]] = {}
    for result in results:
        for fact in result.evidence.facts:
            # Subject-less market buckets are independent source records, while
            # product facts with the same subject/field must agree.
            key = (fact.subject_id or fact.evidence_ids[0], fact.field)
            value = (fact.value, fact.unit)
            owners.setdefault(key, set()).update(result.operation.obligation_ids)
            previous = values.setdefault(key, value)
            if previous != value:
                conflicting.update(owners[key])
                conflicting.update(result.operation.obligation_ids)
    return conflicting


def _output_count(result: ExpertResult, key: str) -> int:
    value = (result.output or {}).get(key)
    return len(value) if isinstance(value, list) else 0


def _plan_trace(
    planned: PlannedTurn,
    all_operations: tuple[RuntimeOperation, ...],
    initial: OperationBatch | None,
    continuation: OperationBatch | None,
) -> PlanTrace:
    initial_steps = tuple(
        _plan_step(operation) for operation in planned.initial_operations
    )
    initial_completed = (
        tuple(result.operation.step_id for result in initial.results)
        if initial is not None
        else ()
    )
    revisions = [
        PlanRevision(
            revision=0,
            reason="initial validated read plan",
            steps=initial_steps,
            executed_step_ids=initial_completed,
        )
    ]
    if continuation is not None:
        new_ids = tuple(result.operation.step_id for result in continuation.results)
        revisions.append(
            PlanRevision(
                revision=1,
                reason="bounded evidence continuation",
                steps=tuple(_plan_step(operation) for operation in all_operations),
                added_read_step_ids=tuple(
                    operation.step_id
                    for operation in all_operations[len(planned.initial_operations) :]
                ),
                executed_step_ids=new_ids,
                reused_step_ids=initial_completed,
            )
        )
    return PlanTrace(
        plan_id=planned.plan_id,
        intent=planned.intent,
        revisions=tuple(revisions),
    )


def _plan_step(operation: RuntimeOperation) -> PlanStep:
    return PlanStep(
        step_id=operation.step_id,
        operation_key=operation.operation_key,
        capability=operation.capability,
        service=operation.service.value,
        depends_on=operation.depends_on,
        data_version_ids=operation.data_version_ids,
    )


def _execution_records(
    results: tuple[ExpertResult, ...],
    reused_step_ids: set[str],
) -> tuple[ExecutionRecord, ...]:
    records: list[ExecutionRecord] = []
    seen: set[str] = set()
    for result in results:
        operation = result.operation
        if operation.operation_key in seen:
            continue
        seen.add(operation.operation_key)
        duration = max(
            0.0,
            (result.completed_at - result.started_at).total_seconds() * 1_000,
        )
        records.append(
            ExecutionRecord(
                execution_id=f"exec_{operation.operation_key[:24]}",
                step_id=operation.step_id,
                operation_key=operation.operation_key,
                capability=operation.capability,
                service=operation.service.value,
                status=result.status,
                started_at=result.started_at,
                completed_at=result.completed_at,
                duration_ms=duration,
                reused=operation.step_id in reused_step_ids,
                error=result.error,
            )
        )
    return tuple(records)


def _needs_entity_clarification(
    planned: PlannedTurn,
    assessment: EvidenceAssessment,
) -> bool:
    missing = set(assessment.missing_obligation_ids)
    entity_kinds = {
        EvidenceObligationKind.CATALOG,
        EvidenceObligationKind.COMPARISON,
        EvidenceObligationKind.REVIEW,
        EvidenceObligationKind.TRUST,
        EvidenceObligationKind.PRICE,
    }
    return not assessment.candidate_product_ids and any(
        obligation.obligation_id in missing and obligation.kind in entity_kinds
        for obligation in planned.obligations
    )


def _clarification_answer(code: str) -> str:
    if code == "candidate_limit_exceeds_five":
        return "Mỗi lượt hỗ trợ tối đa 5 ứng viên; bạn muốn ưu tiên tiêu chí nào?"
    if code == "price_range_invalid":
        return "Khoảng giá chưa hợp lệ; bạn hãy nêu lại mức tối thiểu và tối đa."
    if code in {"price_constraint_ambiguous", "price_constraint_invalid"}:
        return "Mức giá chưa rõ; bạn hãy dùng số VND nguyên hoặc ghi rõ nghìn/triệu."
    if code == "inventory_requires_merchant_mode":
        return "Bạn cần mở hội thoại merchant để xem dữ liệu tồn kho demo."
    if code == "write_action_requires_package_5":
        return (
            "Lượt đọc này chưa thể thực hiện thay đổi; bạn muốn xem dữ liệu "
            "hiện có trước không?"
        )
    if code == "action_confirmation_endpoint_required":
        return "Hãy xác nhận hoặc từ chối bằng nút trên thẻ hành động tương ứng."
    if code == "action_write_permission_required":
        return "Bạn chưa có quyền tạo đề xuất thay đổi trong chế độ hội thoại này."
    if code == "action_mode_mismatch":
        return "Yêu cầu thay đổi này không phù hợp với chế độ hội thoại hiện tại."
    if code == "action_request_ambiguous":
        return "Bạn hãy yêu cầu riêng một thay đổi cần xác nhận trong mỗi lượt."
    if code in {
        "merchant_action_product_required",
        "merchant_action_single_product_required",
    }:
        return "Bạn hãy chọn chính xác một sản phẩm trước khi tạo đề xuất thay đổi."
    if code in {
        "merchant_price_value_required",
        "merchant_price_value_invalid",
        "merchant_price_value_ambiguous",
    }:
        return "Bạn hãy nêu chính xác mức giá mới bằng số VND nguyên."
    if code in {
        "merchant_stock_delta_required",
        "merchant_stock_delta_nonzero",
        "merchant_stock_delta_invalid",
    }:
        return "Bạn hãy nêu chính xác mức tăng hoặc giảm tồn kho khác 0."
    if code == "action_service_unavailable":
        return "Dịch vụ tạo thẻ xác nhận hiện chưa sẵn sàng; vui lòng thử lại sau."
    if code == "checkout_cart_unavailable":
        return "Chưa có giỏ hàng đang hoạt động để tạo đề xuất thanh toán."
    if code == "merchant_offer_unavailable":
        return "Không tìm thấy đúng một offer hiện hành cho sản phẩm đã chọn."
    if code == "action_prerequisite_changed":
        return "Dữ liệu hiện tại đã thay đổi; vui lòng kiểm tra lại trước khi đề xuất."
    if code == "merchant_offer_execution_requires_confirmed_proposal":
        return (
            "Chỉ có thể áp dụng ưu đãi từ một đề xuất đã được xác nhận; "
            "hãy tạo và xác nhận đề xuất trước."
        )
    return "Bạn hãy nêu rõ hơn tên sách, tác giả hoặc tiêu chí cần kiểm tra."


def _proposal_clarification(
    planned: PlannedTurn,
    code: str,
    fallback_reasons: tuple[str, ...],
) -> TurnComputation:
    return TurnComputation(
        result=TurnResult(
            outcome=DialogueOutcome.NEEDS_CLARIFICATION,
            answer=_clarification_answer(code),
            plan=_plan_trace(planned, (), None, None),
            warnings=(code,),
        ),
        fallback_reasons=fallback_reasons,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "GroundedAnswerProducer",
    "V2ReadSupervisor",
    "assess_evidence",
    "merge_tool_evidence",
    "select_grounding_evidence",
]
