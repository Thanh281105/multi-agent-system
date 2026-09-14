"""Bounded, registry-validated planning and one-shot continuation for v2 reads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.shared import (
    ModelRuntime,
    ModelRuntimeError,
    ModelRuntimeMode,
    ReasoningEffort,
    mark_model_call_fallback,
)
from app.shared.budget import BudgetError, current_provider_budget
from app.v2.authorization import ResourceAuthorization, required_scopes_for_mode
from app.v2.contracts import (
    ACTION_PATTERN,
    MAX_ADDED_READ_STEPS,
    MAX_CANDIDATES,
    MAX_EXPERT_STEPS,
    MAX_KNOWLEDGE_RETRIEVALS,
    ConversationMode,
    ProductId,
    StableId,
    V2Contract,
)
from app.v2.history import ContextConstraint, ModelContext
from app.v2.registry import (
    CapabilityEffect,
    MarketDimension,
    V2CapabilityRegistry,
    default_v2_registry,
)
from app.v2.runtime_contracts import (
    EvidenceAssessment,
    EvidenceObligation,
    EvidenceObligationKind,
    RuntimeOperation,
    build_operation_key,
)

_MODEL_INPUT_TOKEN_BOUND = 12_000
_MODEL_OUTPUT_TOKEN_BOUND = 600
_WRITE_WORDS = (
    "thêm vào giỏ",
    "xóa khỏi giỏ",
    "checkout",
    "thanh toán",
    "đặt hàng",
    "đổi giá",
    "cập nhật giá",
    "điều chỉnh tồn",
)
_CONFIRMATION_ONLY_PATTERN = re.compile(
    r"^(?:chấp nhận|confirm(?:ed)?|đồng ý|no|ok(?:ay)?|reject|từ chối|xác nhận|yes)"
    r"(?:\s*(?:,\s*)?(?:please|thanks|thank you|nhé|nha|ạ|cảm ơn))?[.!?]*$",
    re.IGNORECASE,
)
_CHECKOUT_ACTION_PATTERN = re.compile(
    r"^(?:(?:hãy|vui lòng|giúp tôi|tôi muốn|mình muốn)\s+)*"
    r"(?:checkout|thanh toán|đặt hàng|chốt đơn)"
    r"(?:\s+(?:giỏ hàng|đơn hàng|cart)(?:\s+(?:này|của tôi))?)?[.!?]*$",
    re.IGNORECASE,
)
_PRICE_ACTION_MARKER = re.compile(
    r"^(?:(?:hãy|vui lòng|giúp tôi|tôi muốn|mình muốn)\s+)*"
    r"(?:(?:đổi|cập nhật|đặt)\s+(?:mức\s+)?giá|"
    r"(?:set|change|update)\s+(?:the\s+)?price)\b",
    re.IGNORECASE,
)
_PRICE_ACTION_PATTERN = re.compile(
    _PRICE_ACTION_MARKER.pattern + r"(?:\s+(?:sản phẩm|offer|sách)(?:\s+này)?)?"
    r"\s+(?:thành|sang|lên|xuống|to|at|=)\s*"
    r"(?P<amount>[0-9][0-9.,]*)\s*"
    r"(?P<unit>k|nghìn|triệu|tr|đ|vnd)?[.!?]*$",
    re.IGNORECASE,
)
_STOCK_ACTION_MARKER = re.compile(
    r"^(?:(?:hãy|vui lòng|giúp tôi|tôi muốn|mình muốn)\s+)*"
    r"(?:(?:tăng|giảm|điều chỉnh|cập nhật)\s+(?:số lượng\s+)?"
    r"(?:tồn kho|tồn|stock)|(?:increase|decrease|adjust|update)\s+(?:the\s+)?stock)\b",
    re.IGNORECASE,
)
_STOCK_DIRECTION_PATTERN = re.compile(
    r"^(?:(?:hãy|vui lòng|giúp tôi|tôi muốn|mình muốn)\s+)*"
    r"(?P<direction>tăng|giảm|increase|decrease)\s+(?:số lượng\s+)?"
    r"(?:tồn kho|tồn|stock)(?:\s+(?:sản phẩm|offer|sách)(?:\s+này)?)?"
    r"\s+(?:thêm|bớt|by)?\s*(?P<amount>[0-9]+)[.!?]*$",
    re.IGNORECASE,
)
_STOCK_SIGNED_PATTERN = re.compile(
    r"^(?:(?:hãy|vui lòng|giúp tôi|tôi muốn|mình muốn)\s+)*"
    r"(?:điều chỉnh|cập nhật|adjust|update)\s+(?:số lượng\s+)?"
    r"(?:tồn kho|tồn|stock)(?:\s+(?:sản phẩm|offer|sách)(?:\s+này)?)?"
    r"\s+(?:by\s+)?(?P<amount>[+-][0-9]+)[.!?]*$",
    re.IGNORECASE,
)
_COUNT_PATTERN = re.compile(
    r"\b(?P<count>[0-9]{1,3})\s*(?:cuốn|quyển|sách|books?)\b",
    re.IGNORECASE,
)
_MAX_PRICE_PATTERN = re.compile(
    r"(?:dưới|không quá|tối đa|under|maximum|max)\s*"
    r"(?P<amount>[0-9][0-9.,]*)\s*(?P<unit>k|nghìn|triệu|tr|đ|vnd)?",
    re.IGNORECASE,
)
_MIN_PRICE_PATTERN = re.compile(
    r"(?:trên|ít nhất|từ|over|minimum|min)\s*"
    r"(?P<amount>[0-9][0-9.,]*)\s*(?P<unit>k|nghìn|triệu|tr|đ|vnd)?",
    re.IGNORECASE,
)
_PRODUCT_INPUT_CAPABILITIES = frozenset(
    {
        "product.compare",
        "review.retrieve",
        "review.compare",
        "trust.analyze",
        "trust.compare",
    }
)
_CANDIDATE_SOURCE_CAPABILITIES = frozenset(
    {"product.catalog.search", "product.rank", "merchant.catalog.read"}
)
_P4_IMPLEMENTED_READ_CAPABILITIES = frozenset(
    {
        "product.catalog.search",
        "product.compare",
        "product.rank",
        "review.retrieve",
        "review.compare",
        "trust.analyze",
        "trust.compare",
        "market.snapshot",
        "knowledge.retrieve",
        "merchant.catalog.read",
        "merchant.inventory.read",
        "shopper.cart.read",
        "shopper.checkout.preview",
    }
)
_QUOTED_QUERY_PATTERN = re.compile(r"[\"“”'](?P<query>[^\"“”']{2,160})[\"“”']")
_QUERY_PREFIX_PATTERN = re.compile(
    r"^(?:(?:hãy|vui lòng|giúp|cho tôi|mình muốn)\s+)*"
    r"(?:tìm|kiếm|gợi ý|đề xuất|recommend|find|search|so sánh|compare)\s+"
    r"(?:(?:cho tôi|giúp tôi)\s+)?"
    r"(?:[0-9]{1,3}\s*(?:cuốn|quyển|sách|books?)\s+)?"
    r"(?:(?:cuốn|quyển)\s+)?(?:sách|books?)?\s*",
    re.IGNORECASE,
)
_QUERY_TRAILING_CLAUSE_PATTERN = re.compile(
    r"\s*(?:,|;|\bvà\b|\band\b)\s*"
    r"(?:cho biết|xem|kiểm tra|so sánh|compare|review|đánh giá|nhận xét|"
    r"phàn nàn|khiếu nại|chủ đề|nội dung|giá|price)\b.*$",
    re.IGNORECASE,
)
_GENERIC_QUERY_TERMS = frozenset(
    {
        "",
        "sách",
        "book",
        "books",
        "bán tốt",
        "đáng mua",
        "phù hợp",
        "review tốt",
        "đánh giá tốt",
    }
)


class PlanningError(RuntimeError):
    """A safe planning failure that the durable runner may persist."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ModelPlanRejectedError(PlanningError):
    """Required-mode model output failed Python authorization or validation."""


class RuntimeDataVersions(V2Contract):
    """Immutable snapshots pinned for the whole turn."""

    catalog_version_id: StableId
    corpus_version_id: StableId
    index_manifest_id: StableId


class PlanningContext(V2Contract):
    """Current server-owned authority and already-resolved entity boundary."""

    access: ResourceAuthorization
    versions: RuntimeDataVersions
    resolved_product_ids: tuple[ProductId, ...] = Field(
        default=(), max_length=MAX_CANDIDATES
    )
    allowed_source_ids: frozenset[StableId] = frozenset()
    model_context: ModelContext = Field(default_factory=ModelContext)

    @field_validator("resolved_product_ids")
    @classmethod
    def validate_product_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)):
            raise ValueError("resolved product IDs must be unique")
        return value


CapabilityName = Annotated[str, Field(pattern=ACTION_PATTERN)]


class ModelPlanChoice(V2Contract):
    """A bounded selector; it cannot author tool inputs or dependencies."""

    template_id: StableId
    capabilities: tuple[CapabilityName, ...] = Field(
        min_length=1, max_length=MAX_EXPERT_STEPS
    )
    selected_product_ids: tuple[ProductId, ...] = Field(
        default=(), max_length=MAX_CANDIDATES
    )
    candidate_limit: int = Field(
        default=MAX_CANDIDATES,
        strict=True,
        ge=1,
        le=MAX_CANDIDATES,
    )

    @model_validator(mode="after")
    def validate_unique_values(self) -> ModelPlanChoice:
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("model capabilities must be unique")
        if len(self.selected_product_ids) != len(set(self.selected_product_ids)):
            raise ValueError("model product IDs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class PlannedProposal:
    """Server-parsed action semantics; never model-authored action input."""

    kind: Literal["checkout", "merchant_price", "merchant_stock"]
    product_id: ProductId | None = None
    new_price_vnd: int | None = None
    quantity_delta: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "checkout":
            valid = (
                self.product_id is None
                and self.new_price_vnd is None
                and self.quantity_delta is None
            )
        elif self.kind == "merchant_price":
            valid = (
                self.product_id is not None
                and self.new_price_vnd is not None
                and self.quantity_delta is None
            )
        else:
            valid = (
                self.product_id is not None
                and self.new_price_vnd is None
                and self.quantity_delta is not None
                and self.quantity_delta != 0
            )
        if not valid:
            raise ValueError("planned proposal semantics are inconsistent")


@dataclass(frozen=True, slots=True)
class PlannedTurn:
    plan_id: str
    intent: str
    template_id: str
    obligations: tuple[EvidenceObligation, ...]
    desired_capabilities: tuple[str, ...]
    initial_operations: tuple[RuntimeOperation, ...]
    deferred_capabilities: tuple[str, ...]
    candidate_limit: int
    query: str
    catalog_query: str | None = None
    min_price_vnd: int | None = None
    max_price_vnd: int | None = None
    clarification_code: str | None = None
    model_selected_template_id: str | None = None
    fallback_reason: str | None = None
    proposal: PlannedProposal | None = None


@dataclass(frozen=True, slots=True)
class _DeterministicRequest:
    intent: str
    template_id: str
    obligations: tuple[EvidenceObligation, ...]
    capabilities: tuple[str, ...]
    candidate_limit: int
    candidate_limit_explicit: bool
    query: str
    catalog_query: str | None
    min_price_vnd: int | None
    max_price_vnd: int | None
    clarification_code: str | None
    proposal: PlannedProposal | None = None


@dataclass(frozen=True, slots=True)
class _ParsedRequestContext:
    cleaned: str
    candidate_limit: int
    candidate_limit_explicit: bool
    count_error: str | None
    min_price_vnd: int | None
    max_price_vnd: int | None
    price_error: str | None
    catalog_query: str | None


class BoundedV2Planner:
    """Compile only read operations from registry and trusted request context."""

    def __init__(
        self,
        *,
        registry: V2CapabilityRegistry = default_v2_registry,
        model_runtime: ModelRuntime | None = None,
        runtime_mode: ModelRuntimeMode = "off",
        model: str = "gpt-5.4-mini",
        reasoning_effort: ReasoningEffort = "low",
        rag_enabled: bool = True,
    ) -> None:
        if type(rag_enabled) is not bool:
            raise TypeError("rag_enabled must be a bool")
        self.registry = registry
        self.model_runtime = model_runtime
        self.runtime_mode = runtime_mode
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.rag_enabled = rag_enabled

    async def plan(self, message: str, context: PlanningContext) -> PlannedTurn:
        request = _deterministic_request(message, context)
        policy_request = self._apply_rag_policy(request)
        plan_id = _plan_id(
            message,
            context,
            candidate_limit=request.candidate_limit,
        )
        if request.clarification_code is not None:
            return PlannedTurn(
                plan_id=plan_id,
                intent=request.intent,
                template_id=request.template_id,
                obligations=request.obligations,
                desired_capabilities=policy_request.capabilities,
                initial_operations=(),
                deferred_capabilities=policy_request.capabilities,
                candidate_limit=request.candidate_limit,
                query=request.query,
                catalog_query=request.catalog_query,
                min_price_vnd=request.min_price_vnd,
                max_price_vnd=request.max_price_vnd,
                clarification_code=request.clarification_code,
                proposal=request.proposal,
            )

        if request.proposal is not None:
            _authorized_plan_options(
                policy_request,
                context,
                self.registry,
                rag_enabled=self.rag_enabled,
            )

        effective_request = policy_request
        selected_template: str | None = None
        fallback_reason: str | None = None
        if self.runtime_mode != "off" and policy_request.capabilities:
            choice, fallback_reason = await self._model_choice(policy_request, context)
            if choice is not None:
                selected_template = choice.template_id
                if self.runtime_mode in {"hybrid", "required"}:
                    effective_request = replace(
                        policy_request,
                        intent=_intent_for_template(
                            self.registry,
                            context.access.binding.mode,
                            choice.template_id,
                            policy_request.intent,
                        ),
                        template_id=choice.template_id,
                        capabilities=choice.capabilities,
                        candidate_limit=choice.candidate_limit,
                    )

        operations, deferred = (
            ((), ())
            if request.proposal is not None
            else self._compile_initial(effective_request, context)
        )
        return PlannedTurn(
            plan_id=plan_id,
            intent=effective_request.intent,
            template_id=effective_request.template_id,
            obligations=request.obligations,
            desired_capabilities=effective_request.capabilities,
            initial_operations=operations,
            deferred_capabilities=deferred,
            candidate_limit=effective_request.candidate_limit,
            query=request.query,
            catalog_query=request.catalog_query,
            min_price_vnd=request.min_price_vnd,
            max_price_vnd=request.max_price_vnd,
            model_selected_template_id=selected_template,
            fallback_reason=fallback_reason,
            proposal=request.proposal,
        )

    def _apply_rag_policy(
        self, request: _DeterministicRequest
    ) -> _DeterministicRequest:
        if self.rag_enabled:
            return request
        capabilities = tuple(
            capability
            for capability in request.capabilities
            if capability != "knowledge.retrieve"
        )
        model_obligations = tuple(
            obligation
            for obligation in request.obligations
            if any(
                _capability_satisfies(capability, obligation.kind)
                for capability in capabilities
            )
        )
        return replace(
            request,
            capabilities=capabilities,
            obligations=model_obligations,
        )

    def bind_initial_candidates(
        self,
        planned: PlannedTurn,
        candidate_product_ids: tuple[int, ...],
        context: PlanningContext,
        existing_operations: tuple[RuntimeOperation, ...],
    ) -> tuple[RuntimeOperation, ...]:
        """Bind planned dependent reads after the initial catalog result.

        These reads are part of revision zero.  They consume the eight-step cap,
        but do not consume the one evidence-driven revision or its two-read cap.
        """

        if not candidate_product_ids or not planned.deferred_capabilities:
            return ()
        if len(candidate_product_ids) > min(planned.candidate_limit, MAX_CANDIDATES):
            raise PlanningError("resolved_candidate_limit_exceeded")
        context_product_ids = _context_product_ids(
            context,
            candidate_limit=planned.candidate_limit,
        )
        if not set(context_product_ids) <= set(candidate_product_ids):
            raise PlanningError("resolved_context_product_missing")
        bound_context = context.model_copy(
            update={"resolved_product_ids": candidate_product_ids}
        )
        request = _request_from_plan(planned)
        existing_keys = {operation.operation_key for operation in existing_operations}
        candidate_step = next(
            (
                operation.step_id
                for operation in existing_operations
                if operation.capability in _CANDIDATE_SOURCE_CAPABILITIES
            ),
            None,
        )
        bound: list[RuntimeOperation] = []
        for capability in planned.deferred_capabilities:
            minimum = 2 if capability == "product.compare" else 1
            if capability in _PRODUCT_INPUT_CAPABILITIES and (
                len(candidate_product_ids) < minimum
            ):
                continue
            if len(existing_operations) + len(bound) >= MAX_EXPERT_STEPS:
                break
            obligation_ids = tuple(
                obligation.obligation_id
                for obligation in planned.obligations
                if _capability_satisfies(capability, obligation.kind)
            )
            operation = self._compile_operation(
                capability=capability,
                step_number=len(existing_operations) + len(bound) + 1,
                request=request,
                context=bound_context,
                allowed_product_ids=candidate_product_ids,
                depends_on=(candidate_step,) if candidate_step is not None else (),
                obligation_ids=obligation_ids,
            )
            if operation.operation_key in existing_keys:
                continue
            existing_keys.add(operation.operation_key)
            bound.append(operation)
        return tuple(bound)

    def continue_plan(
        self,
        planned: PlannedTurn,
        assessment: EvidenceAssessment,
        context: PlanningContext,
        existing_operations: tuple[RuntimeOperation, ...],
    ) -> tuple[RuntimeOperation, ...]:
        """Compile at most two new reads; old operation keys are never returned."""

        if not assessment.can_continue:
            return ()
        missing = set(assessment.missing_obligation_ids)
        if not missing:
            return ()
        obligations = {
            obligation.obligation_id: obligation for obligation in planned.obligations
        }
        missing_kinds: set[EvidenceObligationKind] = {
            obligations[obligation_id].kind
            for obligation_id in missing
            if obligation_id in obligations
        }
        candidates = assessment.candidate_product_ids
        existing_keys = {operation.operation_key for operation in existing_operations}
        existing_capabilities = [
            operation.capability for operation in existing_operations
        ]
        candidate_step = next(
            (
                operation.step_id
                for operation in existing_operations
                if operation.capability in {"product.catalog.search", "product.rank"}
            ),
            None,
        )
        proposed: list[RuntimeOperation] = []
        knowledge_count = sum(
            operation.capability == "knowledge.retrieve"
            for operation in existing_operations
        )
        for capability in planned.desired_capabilities:
            if not _capability_needed(capability, missing_kinds):
                continue
            repeated_knowledge = (
                capability == "knowledge.retrieve"
                and capability in existing_capabilities
                and knowledge_count < MAX_KNOWLEDGE_RETRIEVALS
            )
            if capability in existing_capabilities and not repeated_knowledge:
                continue
            if capability in _PRODUCT_INPUT_CAPABILITIES:
                minimum = 2 if capability == "product.compare" else 1
                if len(candidates) < minimum:
                    continue
            if capability == "knowledge.retrieve" and knowledge_count >= (
                MAX_KNOWLEDGE_RETRIEVALS
            ):
                continue
            if len(existing_operations) + len(proposed) >= MAX_EXPERT_STEPS:
                break
            if len(proposed) >= MAX_ADDED_READ_STEPS:
                break

            obligation_ids = tuple(
                obligation.obligation_id
                for obligation in planned.obligations
                if obligation.obligation_id in missing
                and _capability_satisfies(capability, obligation.kind)
            )
            depends_on = (
                (candidate_step,)
                if candidate_step is not None
                and capability in _PRODUCT_INPUT_CAPABILITIES
                else ()
            )
            operation = self._compile_operation(
                capability=capability,
                step_number=len(existing_operations) + len(proposed) + 1,
                request=_request_from_plan(planned),
                context=context,
                allowed_product_ids=candidates,
                depends_on=depends_on,
                obligation_ids=obligation_ids,
                expanded_knowledge=repeated_knowledge,
            )
            if operation.operation_key in existing_keys:
                continue
            existing_keys.add(operation.operation_key)
            proposed.append(operation)
            if capability == "knowledge.retrieve":
                knowledge_count += 1
        return tuple(proposed)

    async def _model_choice(
        self,
        request: _DeterministicRequest,
        context: PlanningContext,
    ) -> tuple[ModelPlanChoice | None, str | None]:
        if self.model_runtime is None:
            return self._unavailable_model("model_runtime_unavailable")
        if current_provider_budget() is None:
            return self._unavailable_model("provider_budget_unavailable")

        options = _authorized_plan_options(
            request,
            context,
            self.registry,
            rag_enabled=self.rag_enabled,
        )

        input_payload = json.dumps(
            {
                "mode": context.access.binding.mode.value,
                "request": request.query,
                "authorized_plan_options": [
                    {
                        "template_id": template_id,
                        "capabilities": list(capabilities),
                    }
                    for template_id, capabilities in options
                ],
                "server_resolved_product_ids": list(
                    _request_product_ids(request, context)
                ),
                "planning_context_only": _model_context_projection(
                    context,
                    candidate_limit=request.candidate_limit,
                ),
                "required_obligations": [
                    {
                        "obligation_id": obligation.obligation_id,
                        "kind": obligation.kind,
                        "explicit": obligation.explicit,
                    }
                    for obligation in request.obligations
                ],
                "candidate_limit": request.candidate_limit,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if request.proposal is not None:
            instructions = (
                "Copy the one supplied authorized proposal template, its complete "
                "capability sequence, server-resolved product IDs, and candidate "
                "limit exactly. Do not author parameters, cart or offer IDs, "
                "versions, proposal IDs, approval, confirmation, execution, tools, "
                "or data."
            )
        else:
            instructions = (
                "Select exactly one supplied authorized plan option for this book "
                "read request. Copy its template_id and complete capability "
                "sequence, and copy server-resolved product IDs exactly. You may "
                "lower a non-explicit candidate limit only when the selected plan "
                "still fulfills every required obligation. Do not create IDs, "
                "parameters, actions, tools, or data."
            )
        if _text_token_bound(instructions, input_payload) > _MODEL_INPUT_TOKEN_BOUND:
            raise PlanningError("planning_model_input_too_large")
        try:
            generated = await self.model_runtime.generate_structured(
                stage="v2_planning",
                agent_id="supervisor",
                model=self.model,
                instructions=instructions,
                input_text=input_payload,
                schema=ModelPlanChoice,
                max_output_tokens=_MODEL_OUTPUT_TOKEN_BOUND,
                reasoning_effort=self.reasoning_effort,
            )
        except asyncio.CancelledError:
            raise
        except BudgetError:
            raise
        except ModelRuntimeError as exc:
            if self.runtime_mode == "required":
                raise
            mark_model_call_fallback(exc.metadata, "v2_deterministic_plan")
            return None, exc.code

        try:
            _validate_model_choice(
                generated.value,
                request,
                context,
                options=options,
            )
        except ValueError as exc:
            mark_model_call_fallback(generated.metadata, "v2_plan_rejected")
            if self.runtime_mode == "required":
                raise ModelPlanRejectedError("model_plan_not_authorized") from exc
            return None, "model_plan_not_authorized"

        if self.runtime_mode == "shadow":
            mark_model_call_fallback(generated.metadata, "shadow_mode")
            return generated.value, "shadow_mode"
        return generated.value, None

    def _unavailable_model(
        self,
        reason: str,
    ) -> tuple[None, str]:
        if self.runtime_mode == "required":
            raise ModelPlanRejectedError(reason, retryable=True)
        return None, reason

    def _compile_initial(
        self,
        request: _DeterministicRequest,
        context: PlanningContext,
    ) -> tuple[tuple[RuntimeOperation, ...], tuple[str, ...]]:
        operations: list[RuntimeOperation] = []
        deferred: list[str] = []
        resolves_candidates = any(
            capability in _CANDIDATE_SOURCE_CAPABILITIES
            for capability in request.capabilities
        )
        context_product_ids = _context_product_ids(
            context,
            candidate_limit=request.candidate_limit,
        )
        for capability in request.capabilities:
            if (
                capability in _PRODUCT_INPUT_CAPABILITIES
                or (capability == "knowledge.retrieve" and resolves_candidates)
            ) and not context_product_ids:
                deferred.append(capability)
                continue
            obligation_ids = tuple(
                obligation.obligation_id
                for obligation in request.obligations
                if _capability_satisfies(capability, obligation.kind)
            )
            operation = self._compile_operation(
                capability=capability,
                step_number=len(operations) + 1,
                request=request,
                context=context,
                allowed_product_ids=context_product_ids,
                obligation_ids=obligation_ids,
            )
            if any(
                existing.operation_key == operation.operation_key
                for existing in operations
            ):
                continue
            operations.append(operation)
        return tuple(operations), tuple(deferred)

    def _compile_operation(
        self,
        *,
        capability: str,
        step_number: int,
        request: _DeterministicRequest,
        context: PlanningContext,
        allowed_product_ids: tuple[int, ...],
        depends_on: tuple[str, ...] = (),
        obligation_ids: tuple[str, ...] = (),
        expanded_knowledge: bool = False,
    ) -> RuntimeOperation:
        if capability == "knowledge.retrieve" and not self.rag_enabled:
            raise PlanningError("knowledge_capability_disabled")
        definition = self.registry.capability(capability)
        if definition.effect != CapabilityEffect.READ:
            raise PlanningError("write_capability_forbidden")
        if context.access.binding.mode not in definition.allowed_modes:
            raise PlanningError("capability_mode_forbidden")
        if not definition.required_permissions <= context.access.scopes:
            raise PlanningError("capability_permission_forbidden")
        payload = _operation_payload(
            capability,
            request,
            allowed_product_ids,
            context,
            expanded_knowledge=expanded_knowledge,
        )
        try:
            validated = definition.validate_input(payload)
        except ValueError as exc:
            raise PlanningError("operation_parameters_invalid") from exc
        parameters = validated.model_dump(mode="json")
        _validate_operation_authority(
            capability,
            parameters,
            allowed_product_ids=allowed_product_ids,
            allowed_source_ids=context.allowed_source_ids,
        )
        data_versions = _data_versions(capability, context.versions)
        return RuntimeOperation(
            step_id=f"step_{step_number:03d}",
            capability=capability,
            service=definition.service,
            parameters=parameters,
            depends_on=depends_on,
            data_version_ids=data_versions,
            obligation_ids=obligation_ids,
            operation_key=build_operation_key(
                capability,
                parameters,
                data_versions,
            ),
        )


def _parse_request_context(message: str) -> _ParsedRequestContext:
    cleaned = " ".join(message.split()).strip()
    if not cleaned:
        raise PlanningError("message_empty")
    lowered = cleaned.casefold()
    candidate_limit, candidate_explicit, count_error = _candidate_limit(lowered)
    price_error: str | None = None
    try:
        min_price = _price_constraint(_MIN_PRICE_PATTERN, lowered)
        max_price = _price_constraint(_MAX_PRICE_PATTERN, lowered)
    except PlanningError as exc:
        if exc.code not in {
            "price_constraint_ambiguous",
            "price_constraint_invalid",
        }:
            raise
        min_price = None
        max_price = None
        price_error = exc.code
    if (
        price_error is None
        and min_price is not None
        and max_price is not None
        and min_price > max_price
    ):
        price_error = "price_range_invalid"
    return _ParsedRequestContext(
        cleaned=cleaned,
        candidate_limit=candidate_limit,
        candidate_limit_explicit=candidate_explicit,
        count_error=count_error,
        min_price_vnd=min_price,
        max_price_vnd=max_price,
        price_error=price_error,
        catalog_query=_extract_catalog_query(cleaned),
    )


def context_constraints_from_message(message: str) -> tuple[ContextConstraint, ...]:
    """Project only deterministically parsed request filters into history context."""

    cleaned = message.strip()
    if (
        _is_confirmation_only(cleaned)
        or _CHECKOUT_ACTION_PATTERN.fullmatch(cleaned) is not None
        or _PRICE_ACTION_MARKER.search(cleaned) is not None
        or _STOCK_ACTION_MARKER.search(cleaned) is not None
    ):
        return ()
    parsed = _parse_request_context(message)
    constraints: list[ContextConstraint] = []
    if parsed.candidate_limit_explicit and parsed.count_error is None:
        constraints.append(
            ContextConstraint(key="candidate_limit", value=parsed.candidate_limit)
        )
    if parsed.price_error is None:
        if parsed.min_price_vnd is not None:
            constraints.append(
                ContextConstraint(key="min_price_vnd", value=parsed.min_price_vnd)
            )
        if parsed.max_price_vnd is not None:
            constraints.append(
                ContextConstraint(key="max_price_vnd", value=parsed.max_price_vnd)
            )
    if parsed.catalog_query is not None:
        constraints.append(
            ContextConstraint(key="catalog_query", value=parsed.catalog_query)
        )
    return tuple(constraints)


def _deterministic_request(
    message: str,
    context: PlanningContext,
) -> _DeterministicRequest:
    action_request = _deterministic_action_request(message, context)
    if action_request is not None:
        return action_request
    parsed_context = _parse_request_context(message)
    cleaned = parsed_context.cleaned
    lowered = cleaned.casefold()
    candidate_limit = parsed_context.candidate_limit
    candidate_explicit = parsed_context.candidate_limit_explicit
    count_error = parsed_context.count_error
    min_price = parsed_context.min_price_vnd
    max_price = parsed_context.max_price_vnd
    price_error = parsed_context.price_error
    catalog_query = parsed_context.catalog_query
    historical = {
        constraint.key: constraint.value
        for constraint in context.model_context.active_constraints
    }
    if not candidate_explicit:
        historical_limit = historical.get("candidate_limit")
        if type(historical_limit) is int and 1 <= historical_limit <= MAX_CANDIDATES:
            candidate_limit = historical_limit
    if min_price is None and max_price is None and price_error is None:
        historical_min = historical.get("min_price_vnd")
        historical_max = historical.get("max_price_vnd")
        if historical_max is None:
            historical_max = historical.get("max_budget_vnd")
        if type(historical_min) is int and historical_min >= 0:
            min_price = historical_min
        if type(historical_max) is int and historical_max >= 0:
            max_price = historical_max
    if catalog_query is None:
        historical_query = historical.get("catalog_query")
        if isinstance(historical_query, str) and historical_query:
            catalog_query = historical_query[:300]
    write_requested = any(word in lowered for word in _WRITE_WORDS)
    has_compare = _contains(lowered, "so sánh", "compare", "khác nhau")
    has_recommendation = _contains(
        lowered,
        "gợi ý",
        "đề xuất",
        "recommend",
        "nên đọc",
    )
    has_review = _contains(lowered, "review", "đánh giá", "nhận xét")
    has_trust = _contains(lowered, "complaint", "phàn nàn", "khiếu nại")
    has_knowledge = _contains(
        lowered,
        "chủ đề",
        "nội dung",
        "nói về",
        "kiến thức",
        "tiểu sử",
        "knowledge",
    )
    has_market = _contains(lowered, "thị trường", "market", "thống kê")
    has_inventory = _contains(lowered, "tồn kho", "inventory", "còn hàng")
    has_price = (
        _contains(
            lowered,
            "giá",
            "price",
            "ngân sách",
            "vnd",
            "₫",
        )
        or min_price is not None
        or max_price is not None
    )

    mode = context.access.binding.mode
    clarification = count_error or price_error
    if write_requested:
        clarification = clarification or "write_action_requires_package_5"
    if has_inventory and mode != ConversationMode.MERCHANT:
        clarification = clarification or "inventory_requires_merchant_mode"

    context_product_ids = _context_product_ids(
        context,
        candidate_limit=candidate_limit,
    )
    candidate_scope = (
        len(context_product_ids) if context_product_ids else candidate_limit
    )
    comparison_required = has_compare or (has_recommendation and candidate_scope >= 2)
    if has_compare and candidate_scope < 2:
        clarification = clarification or "comparison_requires_two_candidates"

    capabilities_list: list[str] = []
    if has_market:
        intent = "market"
        template_id = "runtime_market"
        capabilities_list.append("market.snapshot")
    elif has_inventory and mode == ConversationMode.MERCHANT:
        intent = "merchant.read"
        template_id = "merchant_read"
        capabilities_list.extend(("merchant.catalog.read", "merchant.inventory.read"))
    elif has_knowledge and (
        has_compare or has_recommendation or has_review or has_trust or has_price
    ):
        intent = "catalog_knowledge"
        template_id = f"{mode.value}_catalog_knowledge"
    elif has_knowledge:
        intent = "knowledge"
        template_id = f"{mode.value}_knowledge"
    elif has_recommendation or has_review or has_trust:
        intent = "recommendation"
        template_id = f"{mode.value}_recommendation"
    elif has_compare:
        intent = "compare"
        template_id = f"{mode.value}_compare"
    else:
        intent = "catalog"
        template_id = f"{mode.value}_catalog"

    if not has_market and not (has_inventory and mode == ConversationMode.MERCHANT):
        needs_candidates = (
            not has_knowledge
            or has_compare
            or has_recommendation
            or has_review
            or has_trust
            or has_price
        )
        if needs_candidates:
            capabilities_list.append(
                "product.rank" if has_recommendation else "product.catalog.search"
            )
        if has_compare or (
            comparison_required and "product.rank" not in capabilities_list
        ):
            capabilities_list.append("product.compare")
        if has_review or has_recommendation:
            capabilities_list.append(
                "review.compare" if candidate_scope >= 2 else "review.retrieve"
            )
        if has_trust or has_recommendation:
            capabilities_list.append(
                "trust.compare" if candidate_scope >= 2 else "trust.analyze"
            )
        if has_knowledge:
            capabilities_list.append("knowledge.retrieve")
    elif has_review or has_trust or has_knowledge:
        # Mixed market/merchant requests retain every explicit read obligation.
        if "merchant.catalog.read" not in capabilities_list:
            capabilities_list.append("product.catalog.search")
        if has_review:
            capabilities_list.append(
                "review.compare" if candidate_scope >= 2 else "review.retrieve"
            )
        if has_trust:
            capabilities_list.append(
                "trust.compare" if candidate_scope >= 2 else "trust.analyze"
            )
        if has_knowledge:
            capabilities_list.append("knowledge.retrieve")
    capabilities = tuple(dict.fromkeys(capabilities_list))

    obligation_specs: list[tuple[EvidenceObligationKind, bool, str]] = []
    if intent in {"catalog", "compare", "recommendation", "catalog_knowledge"}:
        obligation_specs.append(
            (EvidenceObligationKind.CATALOG, True, "catalog candidates")
        )
    if comparison_required:
        obligation_specs.append(
            (EvidenceObligationKind.COMPARISON, has_compare, "candidate comparison")
        )
    if has_price:
        obligation_specs.append((EvidenceObligationKind.PRICE, True, "price"))
    if has_review or has_recommendation:
        obligation_specs.append(
            (
                EvidenceObligationKind.REVIEW,
                has_review,
                "snapshot review evidence",
            )
        )
    if has_trust or has_recommendation:
        obligation_specs.append(
            (
                EvidenceObligationKind.TRUST,
                has_trust,
                "bounded complaint heuristics",
            )
        )
    if has_knowledge:
        obligation_specs.append(
            (EvidenceObligationKind.KNOWLEDGE, True, "external knowledge evidence")
        )
    if has_market:
        obligation_specs.append(
            (EvidenceObligationKind.MARKET, True, "snapshot market metric")
        )
    if has_inventory:
        obligation_specs.append(
            (EvidenceObligationKind.INVENTORY, True, "demo inventory snapshot")
        )
    if not obligation_specs:
        obligation_specs.append(
            (EvidenceObligationKind.CATALOG, True, "catalog candidates")
        )
    obligations = tuple(
        EvidenceObligation(
            obligation_id=f"obl_{kind}",
            kind=kind,
            description=description,
            explicit=explicit,
            product_ids=context_product_ids,
        )
        for kind, explicit, description in dict.fromkeys(obligation_specs)
    )
    if (
        any(
            capability in {"product.catalog.search", "product.rank"}
            for capability in capabilities
        )
        and catalog_query is None
        and min_price is None
        and max_price is None
    ):
        catalog_query = "sách"
    return _DeterministicRequest(
        intent=intent,
        template_id=template_id,
        obligations=obligations,
        capabilities=capabilities,
        candidate_limit=candidate_limit,
        candidate_limit_explicit=candidate_explicit,
        query=cleaned[:500],
        catalog_query=catalog_query,
        min_price_vnd=min_price,
        max_price_vnd=max_price,
        clarification_code=clarification,
    )


def _deterministic_action_request(
    message: str,
    context: PlanningContext,
) -> _DeterministicRequest | None:
    cleaned = message.strip()
    if _is_confirmation_only(cleaned):
        return _action_clarification_request(
            cleaned,
            "action_confirmation_endpoint_required",
        )

    checkout = _CHECKOUT_ACTION_PATTERN.fullmatch(cleaned) is not None
    price_marker = _PRICE_ACTION_MARKER.search(cleaned) is not None
    stock_marker = _STOCK_ACTION_MARKER.search(cleaned) is not None
    if sum((checkout, price_marker, stock_marker)) > 1:
        return _action_clarification_request(cleaned, "action_request_ambiguous")
    if not checkout and not price_marker and not stock_marker:
        return None

    mode = context.access.binding.mode
    if checkout:
        if mode != ConversationMode.SHOPPER:
            return _action_clarification_request(cleaned, "action_mode_mismatch")
        if (
            not required_scopes_for_mode(
                ConversationMode.SHOPPER,
                write=True,
            )
            <= context.access.scopes
        ):
            return _action_clarification_request(
                cleaned,
                "action_write_permission_required",
            )
        return _DeterministicRequest(
            intent="checkout.proposal",
            template_id="shopper_checkout_proposal",
            obligations=(),
            capabilities=(
                "shopper.cart.read",
                "shopper.checkout.preview",
                "shopper.checkout.propose",
            ),
            candidate_limit=MAX_CANDIDATES,
            candidate_limit_explicit=False,
            query=cleaned[:500],
            catalog_query=None,
            min_price_vnd=None,
            max_price_vnd=None,
            clarification_code=None,
            proposal=PlannedProposal(kind="checkout"),
        )

    if mode != ConversationMode.MERCHANT:
        return _action_clarification_request(cleaned, "action_mode_mismatch")
    if (
        not required_scopes_for_mode(
            ConversationMode.MERCHANT,
            write=True,
        )
        <= context.access.scopes
    ):
        return _action_clarification_request(
            cleaned,
            "action_write_permission_required",
        )
    product_ids = context.resolved_product_ids
    if not product_ids:
        return _action_clarification_request(
            cleaned,
            "merchant_action_product_required",
        )
    if len(product_ids) != 1:
        return _action_clarification_request(
            cleaned,
            "merchant_action_single_product_required",
        )
    product_id = product_ids[0]

    proposal: PlannedProposal
    if price_marker:
        match = _PRICE_ACTION_PATTERN.fullmatch(cleaned)
        if match is None:
            return _action_clarification_request(
                cleaned,
                "merchant_price_value_required",
            )
        try:
            amount = _action_price_vnd(match.group("amount"), match.group("unit"))
        except PlanningError as exc:
            return _action_clarification_request(cleaned, exc.code)
        proposal = PlannedProposal(
            kind="merchant_price",
            product_id=product_id,
            new_price_vnd=amount,
        )
    else:
        direction = _STOCK_DIRECTION_PATTERN.fullmatch(cleaned)
        signed = _STOCK_SIGNED_PATTERN.fullmatch(cleaned)
        if direction is None and signed is None:
            return _action_clarification_request(
                cleaned,
                "merchant_stock_delta_required",
            )
        if direction is not None:
            amount = int(direction.group("amount"))
            delta = (
                -amount
                if direction.group("direction").casefold() in {"giảm", "decrease"}
                else amount
            )
        else:
            assert signed is not None
            delta = int(signed.group("amount"))
        if delta == 0:
            return _action_clarification_request(
                cleaned,
                "merchant_stock_delta_nonzero",
            )
        if not -1_000_000 <= delta <= 1_000_000:
            return _action_clarification_request(
                cleaned,
                "merchant_stock_delta_invalid",
            )
        proposal = PlannedProposal(
            kind="merchant_stock",
            product_id=product_id,
            quantity_delta=delta,
        )

    return _DeterministicRequest(
        intent="merchant.proposal",
        template_id="merchant_proposal",
        obligations=(),
        capabilities=("merchant.inventory.read", "merchant.offer.propose"),
        candidate_limit=1,
        candidate_limit_explicit=True,
        query=cleaned[:500],
        catalog_query=None,
        min_price_vnd=None,
        max_price_vnd=None,
        clarification_code=None,
        proposal=proposal,
    )


def _action_clarification_request(
    message: str,
    code: str,
) -> _DeterministicRequest:
    return _DeterministicRequest(
        intent="action.clarification",
        template_id="action_clarification",
        obligations=(),
        capabilities=(),
        candidate_limit=MAX_CANDIDATES,
        candidate_limit_explicit=False,
        query=message[:500],
        catalog_query=None,
        min_price_vnd=None,
        max_price_vnd=None,
        clarification_code=code,
    )


def _action_price_vnd(amount_text: str, unit_text: str | None) -> int:
    unit = (unit_text or "").casefold()
    try:
        number = _price_decimal(
            amount_text.rstrip(".,"),
            fractional_suffix=unit in {"k", "nghìn", "triệu", "tr"},
        )
    except PlanningError as exc:
        if exc.code == "price_constraint_ambiguous":
            raise PlanningError("merchant_price_value_ambiguous") from exc
        raise
    except (InvalidOperation, ValueError) as exc:
        raise PlanningError("merchant_price_value_invalid") from exc
    if unit in {"k", "nghìn"}:
        number *= Decimal(1_000)
    elif unit in {"triệu", "tr"}:
        number *= Decimal(1_000_000)
    if number != number.to_integral_value():
        raise PlanningError("merchant_price_value_ambiguous")
    amount = int(number)
    if not 0 < amount <= 10_000_000_000:
        raise PlanningError("merchant_price_value_invalid")
    return amount


def _is_confirmation_only(message: str) -> bool:
    return _CONFIRMATION_ONLY_PATTERN.fullmatch(message.strip()) is not None


def _request_from_plan(planned: PlannedTurn) -> _DeterministicRequest:
    return _DeterministicRequest(
        intent=planned.intent,
        template_id=planned.template_id,
        obligations=planned.obligations,
        capabilities=planned.desired_capabilities,
        candidate_limit=planned.candidate_limit,
        candidate_limit_explicit=False,
        query=planned.query,
        catalog_query=planned.catalog_query,
        min_price_vnd=planned.min_price_vnd,
        max_price_vnd=planned.max_price_vnd,
        clarification_code=planned.clarification_code,
        proposal=planned.proposal,
    )


def _operation_payload(
    capability: str,
    request: _DeterministicRequest,
    product_ids: tuple[int, ...],
    context: PlanningContext,
    *,
    expanded_knowledge: bool,
) -> dict[str, object]:
    if capability in {"product.catalog.search", "product.rank"}:
        return {
            "query": request.catalog_query,
            "min_price_vnd": request.min_price_vnd,
            "max_price_vnd": request.max_price_vnd,
            "candidate_limit": request.candidate_limit,
        }
    if capability in _PRODUCT_INPUT_CAPABILITIES:
        return {"product_ids": list(product_ids)}
    if capability == "knowledge.retrieve":
        return {
            "query": request.query,
            "product_ids": list(product_ids),
            "requested_source_ids": None,
            "top_k": 8 if expanded_knowledge else 6,
        }
    if capability == "market.snapshot":
        lowered = request.query.casefold()
        if "tác giả" in lowered or "author" in lowered:
            dimension = MarketDimension.AUTHOR
        elif "nhà xuất bản" in lowered or "publisher" in lowered:
            dimension = MarketDimension.PUBLISHER
        elif "giá" in lowered or "price" in lowered:
            dimension = MarketDimension.PRICE
        elif "rating" in lowered or "đánh giá" in lowered:
            dimension = MarketDimension.RATING
        else:
            dimension = MarketDimension.CATEGORY
        return {"dimension": dimension.value}
    if capability == "shopper.cart.read":
        return {}
    if capability in {"merchant.catalog.read", "merchant.inventory.read"}:
        return {"product_ids": list(product_ids)}
    raise PlanningError("capability_parameters_unsupported")


def _validate_operation_authority(
    capability: str,
    parameters: dict[str, object],
    *,
    allowed_product_ids: tuple[int, ...],
    allowed_source_ids: frozenset[str],
) -> None:
    raw_product_ids = parameters.get("product_ids", [])
    if not isinstance(raw_product_ids, list):
        raise PlanningError("operation_product_ids_invalid")
    product_ids = tuple(raw_product_ids)
    if any(
        not isinstance(product_id, int) or isinstance(product_id, bool)
        for product_id in product_ids
    ):
        raise PlanningError("operation_product_ids_invalid")
    if not set(product_ids) <= set(allowed_product_ids):
        raise PlanningError("operation_product_id_unresolved")
    requested_sources = parameters.get("requested_source_ids")
    if requested_sources is not None:
        if not isinstance(requested_sources, (list, set, frozenset, tuple)):
            raise PlanningError("operation_source_ids_invalid")
        if not set(requested_sources) <= allowed_source_ids:
            raise PlanningError("operation_source_id_forbidden")
    if capability in _PRODUCT_INPUT_CAPABILITIES and not product_ids:
        raise PlanningError("operation_product_ids_required")


def _validate_model_choice(
    choice: ModelPlanChoice,
    request: _DeterministicRequest,
    context: PlanningContext,
    *,
    options: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    context_product_ids = _request_product_ids(request, context)
    if (choice.template_id, choice.capabilities) not in options:
        raise ValueError("model plan option is not authorized")
    if choice.selected_product_ids != context_product_ids:
        raise ValueError("model product IDs are not the resolved context")
    if choice.candidate_limit > request.candidate_limit:
        raise ValueError("model cannot expand the candidate limit")
    if choice.candidate_limit < len(context_product_ids):
        raise ValueError("model cannot drop resolved products")
    if (request.proposal is not None or request.candidate_limit_explicit) and (
        choice.candidate_limit != request.candidate_limit
    ):
        raise ValueError("model cannot change the required candidate count")
    for obligation in request.obligations:
        if not any(
            _capability_satisfies(capability, obligation.kind)
            for capability in choice.capabilities
        ):
            raise ValueError("model plan omits a required evidence obligation")
    if (
        any(
            obligation.kind == EvidenceObligationKind.COMPARISON
            for obligation in request.obligations
        )
        and choice.candidate_limit < 2
    ):
        raise ValueError("model plan cannot compare fewer than two candidates")


def _authorized_plan_options(
    request: _DeterministicRequest,
    context: PlanningContext,
    registry: V2CapabilityRegistry,
    *,
    rag_enabled: bool = True,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if request.proposal is not None:
        template = next(
            (
                item
                for item in registry.templates_for_mode(context.access.binding.mode)
                if item.template_id == request.template_id
            ),
            None,
        )
        if template is None or template.capabilities != request.capabilities:
            raise PlanningError("no_authorized_proposal_plan")
        for capability in template.capabilities:
            definition = registry.capability(capability)
            if (
                definition.effect == CapabilityEffect.EXECUTE
                or definition.effect
                not in {CapabilityEffect.READ, CapabilityEffect.PROPOSAL}
                or context.access.binding.mode not in definition.allowed_modes
                or not definition.required_permissions <= context.access.scopes
            ):
                raise PlanningError("no_authorized_proposal_plan")
        if not any(
            registry.capability(capability).effect == CapabilityEffect.PROPOSAL
            for capability in template.capabilities
        ):
            raise PlanningError("no_authorized_proposal_plan")
        return ((template.template_id, template.capabilities),)

    authorized: list[tuple[str, tuple[str, ...]]] = []
    templates = registry.templates_for_mode(context.access.binding.mode)
    candidates = [
        (
            template.template_id,
            tuple(
                capability
                for capability in template.capabilities
                if rag_enabled or capability != "knowledge.retrieve"
            ),
        )
        for template in templates
        if set(template.capabilities) <= _P4_IMPLEMENTED_READ_CAPABILITIES
    ]
    for template_id, template_capabilities in candidates:
        capabilities = list(template_capabilities)
        for obligation in request.obligations:
            if any(
                _capability_satisfies(capability, obligation.kind)
                for capability in capabilities
            ):
                continue
            required = next(
                (
                    capability
                    for capability in request.capabilities
                    if capability in _P4_IMPLEMENTED_READ_CAPABILITIES
                    and _capability_satisfies(capability, obligation.kind)
                ),
                None,
            )
            if required is None:
                capabilities = []
                break
            capabilities.append(required)
        normalized = _normalize_capability_sequence(capabilities)
        if not normalized or len(normalized) > MAX_EXPERT_STEPS:
            continue
        valid = True
        for capability in normalized:
            definition = registry.capability(capability)
            if (
                definition.effect != CapabilityEffect.READ
                or context.access.binding.mode not in definition.allowed_modes
                or not definition.required_permissions <= context.access.scopes
            ):
                valid = False
                break
        if not valid:
            continue
        if any(
            not any(
                _capability_satisfies(capability, obligation.kind)
                for capability in normalized
            )
            for obligation in request.obligations
        ):
            continue
        option = (template_id, normalized)
        if option not in authorized:
            authorized.append(option)
    if not authorized:
        raise PlanningError("no_authorized_read_plan")
    return tuple(authorized)


def _normalize_capability_sequence(capabilities: list[str]) -> tuple[str, ...]:
    priority = {
        "product.catalog.search": 0,
        "product.rank": 0,
        "merchant.catalog.read": 0,
        "product.compare": 1,
        "review.retrieve": 2,
        "review.compare": 2,
        "trust.analyze": 3,
        "trust.compare": 3,
        "merchant.inventory.read": 4,
        "shopper.cart.read": 4,
        "shopper.checkout.preview": 4,
        "market.snapshot": 5,
        "knowledge.retrieve": 6,
    }
    unique = tuple(dict.fromkeys(capabilities))
    return tuple(
        capability
        for _, capability in sorted(
            enumerate(unique),
            key=lambda item: (priority.get(item[1], 99), item[0]),
        )
    )


def _intent_for_template(
    registry: V2CapabilityRegistry,
    mode: ConversationMode,
    template_id: str,
    fallback: str,
) -> str:
    return next(
        (
            template.intent
            for template in registry.templates_for_mode(mode)
            if template.template_id == template_id
        ),
        fallback,
    )


def _data_versions(
    capability: str,
    versions: RuntimeDataVersions,
) -> tuple[str, ...]:
    if capability == "knowledge.retrieve":
        return (
            versions.catalog_version_id,
            versions.corpus_version_id,
            versions.index_manifest_id,
        )
    return (versions.catalog_version_id,)


def _capability_satisfies(
    capability: str,
    obligation_kind: EvidenceObligationKind,
) -> bool:
    mapping: dict[EvidenceObligationKind, set[str]] = {
        EvidenceObligationKind.CATALOG: {
            "product.catalog.search",
            "product.rank",
            "merchant.catalog.read",
        },
        EvidenceObligationKind.COMPARISON: {"product.compare", "product.rank"},
        EvidenceObligationKind.PRICE: {
            "product.catalog.search",
            "product.rank",
            "product.compare",
            "merchant.catalog.read",
        },
        EvidenceObligationKind.REVIEW: {"review.retrieve", "review.compare"},
        EvidenceObligationKind.TRUST: {"trust.analyze", "trust.compare"},
        EvidenceObligationKind.KNOWLEDGE: {"knowledge.retrieve"},
        EvidenceObligationKind.MARKET: {"market.snapshot"},
        EvidenceObligationKind.INVENTORY: {"merchant.inventory.read"},
    }
    return capability in mapping.get(obligation_kind, set())


def _capability_needed(
    capability: str,
    missing_kinds: set[EvidenceObligationKind],
) -> bool:
    return any(_capability_satisfies(capability, kind) for kind in missing_kinds)


def _candidate_limit(message: str) -> tuple[int, bool, str | None]:
    match = _COUNT_PATTERN.search(message)
    if match is None:
        return MAX_CANDIDATES, False, None
    value = int(match.group("count"))
    if not 1 <= value <= MAX_CANDIDATES:
        return MAX_CANDIDATES, True, "candidate_limit_exceeds_five"
    return value, True, None


def _price_constraint(pattern: re.Pattern[str], message: str) -> int | None:
    match = pattern.search(message)
    if match is None:
        return None
    amount_text = match.group("amount").rstrip(".,")
    unit = (match.group("unit") or "").casefold()
    try:
        number = _price_decimal(
            amount_text, fractional_suffix=unit in {"k", "nghìn", "triệu", "tr"}
        )
    except (InvalidOperation, ValueError) as exc:
        raise PlanningError("price_constraint_invalid") from exc
    if unit in {"k", "nghìn"}:
        number *= Decimal(1_000)
    elif unit in {"triệu", "tr"}:
        number *= Decimal(1_000_000)
    if number != number.to_integral_value():
        raise PlanningError("price_constraint_ambiguous")
    amount = int(number)
    if not 0 < amount <= 10_000_000_000:
        raise PlanningError("price_constraint_invalid")
    return amount


def _price_decimal(raw: str, *, fractional_suffix: bool) -> Decimal:
    if not raw or not raw[0].isdigit() or not raw[-1].isdigit():
        raise ValueError("price amount is malformed")
    separators = [character for character in raw if character in {".", ","}]
    if not separators:
        return Decimal(raw)
    if any(
        not character.isdigit() and character not in {".", ","} for character in raw
    ):
        raise ValueError("price amount contains unsupported characters")

    if fractional_suffix:
        if "." in raw and "," in raw:
            decimal_separator = "." if raw.rfind(".") > raw.rfind(",") else ","
            grouping_separator = "," if decimal_separator == "." else "."
            normalized = raw.replace(grouping_separator, "").replace(
                decimal_separator, "."
            )
            return Decimal(normalized)
        separator = separators[0]
        if raw.count(separator) == 1:
            return Decimal(raw.replace(separator, "."))
        groups = raw.split(separator)
        if not groups[0] or any(len(group) != 3 for group in groups[1:]):
            raise ValueError("price grouping is malformed")
        return Decimal("".join(groups))

    if "." in raw and "," in raw:
        raise PlanningError("price_constraint_ambiguous")
    separator = separators[0]
    groups = raw.split(separator)
    if not groups[0] or any(len(group) != 3 for group in groups[1:]):
        raise PlanningError("price_constraint_ambiguous")
    return Decimal("".join(groups))


def _extract_catalog_query(message: str) -> str | None:
    quoted = _QUOTED_QUERY_PATTERN.search(message)
    if quoted is not None:
        return quoted.group("query").strip()[:300]

    candidate = _QUERY_PREFIX_PATTERN.sub("", message.strip(), count=1)
    candidate = re.sub(
        r"^(?:chủ đề|nội dung)\s+(?:của\s+)?",
        "",
        candidate,
        count=1,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"^(?:sách|cuốn sách|quyển sách)\s+",
        "",
        candidate,
        count=1,
        flags=re.IGNORECASE,
    )
    candidate = _QUERY_TRAILING_CLAUSE_PATTERN.sub("", candidate, count=1)
    candidate = _MIN_PRICE_PATTERN.sub("", candidate)
    candidate = _MAX_PRICE_PATTERN.sub("", candidate)
    candidate = _COUNT_PATTERN.sub("", candidate)
    candidate = re.sub(
        r"\b(?:của|by)\s+",
        "",
        candidate,
        count=1,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"\b(?:giá|price|ngân sách|rating|bán tốt|đáng mua|phù hợp)\b.*$",
        "",
        candidate,
        count=1,
        flags=re.IGNORECASE,
    )
    cleaned = candidate.strip(" ,.;:?!-")
    if cleaned.casefold() in _GENERIC_QUERY_TERMS:
        return None
    return cleaned[:300] or None


def _contains(message: str, *needles: str) -> bool:
    return any(needle in message for needle in needles)


def _context_product_ids(
    context: PlanningContext,
    *,
    candidate_limit: int = MAX_CANDIDATES,
) -> tuple[ProductId, ...]:
    if context.resolved_product_ids:
        return context.resolved_product_ids
    return context.model_context.referenced_product_ids[
        : min(candidate_limit, MAX_CANDIDATES)
    ]


def _request_product_ids(
    request: _DeterministicRequest,
    context: PlanningContext,
) -> tuple[ProductId, ...]:
    if request.proposal is None:
        return _context_product_ids(
            context,
            candidate_limit=request.candidate_limit,
        )
    if request.proposal.product_id is None:
        return ()
    return (request.proposal.product_id,)


def _model_context_projection(
    context: PlanningContext,
    *,
    candidate_limit: int = MAX_CANDIDATES,
) -> dict[str, object]:
    """Bounded planning hints; never evidence or answer-supporting material."""

    constraints = [
        {
            "key": item.key,
            "value": item.value[:160] if isinstance(item.value, str) else item.value,
        }
        for item in context.model_context.active_constraints
    ]
    recent_requests = [
        turn.user_message[:300]
        for turn in context.model_context.recent_turns
        if turn.user_message is not None
    ]
    return {
        "notice": "planning context only; never evidence or citation support",
        "recent_user_requests": recent_requests,
        "active_constraints": constraints,
        "referenced_product_ids": list(
            _context_product_ids(context, candidate_limit=candidate_limit)
        ),
    }


def _plan_id(
    message: str,
    context: PlanningContext,
    *,
    candidate_limit: int = MAX_CANDIDATES,
) -> str:
    canonical = json.dumps(
        {
            "message": message,
            "mode": context.access.binding.mode.value,
            "versions": context.versions.model_dump(mode="json"),
            "resolved_product_ids": _context_product_ids(
                context,
                candidate_limit=candidate_limit,
            ),
            "planning_constraints": [
                item.model_dump(mode="json")
                for item in context.model_context.active_constraints
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"plan_{hashlib.sha256(canonical).hexdigest()[:24]}"


def _text_token_bound(*values: str) -> int:
    # UTF-8 bytes are a conservative upper bound for tokenizer output.
    return sum(len(value.encode("utf-8")) for value in values)


__all__ = [
    "BoundedV2Planner",
    "context_constraints_from_message",
    "ModelPlanChoice",
    "ModelPlanRejectedError",
    "PlannedTurn",
    "PlanningContext",
    "PlanningError",
    "RuntimeDataVersions",
]
