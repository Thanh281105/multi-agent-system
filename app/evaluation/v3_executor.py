"""Concrete Package 7 observation execution over the durable v2 SQL path."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from time import monotonic
from typing import Any, cast

from sqlalchemy import delete, select

from app.db.v2_repository import canonical_turn_id
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_gold import (
    LoadedEvaluationGoldV3,
    SandboxFixtureV3,
    SandboxOfferProposalV3,
)
from app.evaluation.v3_models import EvaluationProtocolV3, ScheduledTurnKindV3
from app.evaluation.v3_runner import (
    EmbeddingCallEvidenceV3,
    EvaluationCaseV3,
    EvaluationUserTurnV3,
    InitialStateResetReceiptV3,
    LedgerEventEvidenceV3,
    LedgerEventKindV3,
    ModelCallEvidenceV3,
    ObservationExecutionContextV3,
    ObservationExecutionFailureV3,
    RetryEvidenceV3,
    SandboxFixtureAdapterV3,
    UserTurnExecutionRequestV3,
    UserTurnExecutionResultV3,
    initial_state_reset_receipt_v3,
)
from app.evaluation.v3_variant_runtime import (
    EvaluationV3VariantComposer,
    EvaluationV3VariantRuntime,
)
from app.models.v2 import (
    V2ActionAudit,
    V2ActionIdempotency,
    V2Cart,
    V2CartLine,
    V2Conversation,
    V2Offer,
    V2Order,
    V2Preference,
    V2Proposal,
    V2StepResult,
    V2Turn,
)
from app.shared import ModelRuntime
from app.shared.budget import (
    DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_GENERATION_CALLS,
    DEFAULT_MAX_PROVIDER_ATTEMPTS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TURN_LIMIT_NANO_USD,
    BudgetPurpose,
    ProviderAttemptSnapshot,
    ProviderBudgetContext,
    nano_usd_to_usd,
)
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import (
    MAX_KNOWLEDGE_RETRIEVALS,
    ChatRequest,
    ConversationMode,
    TurnStatus,
)
from app.v2.planning import PlanningContext, ResolvedMerchantTarget
from app.v2.runtime import V2ServiceGraph

_STORE_ID = "demo"


def build_evaluation_cases_v3(
    loaded: LoadedEvaluationGoldV3,
    protocol: EvaluationProtocolV3,
) -> dict[str, EvaluationCaseV3]:
    """Adapt the exact frozen pilot roster while rechecking all public bindings."""

    if protocol.assets.gold_sha256 != loaded.gold_sha256:
        raise ValueError("protocol gold binding differs from loaded evaluation gold")
    if protocol.assets.split_sha256 != loaded.split_sha256:
        raise ValueError("protocol split binding differs from loaded evaluation gold")
    pilot_ids = tuple(item.case_id for item in protocol.pilot_cases)
    if pilot_ids != loaded.gold.frozen_pilot_ids:
        raise ValueError("protocol pilot roster differs from loaded evaluation gold")
    conversations = {item.conversation_id: item for item in loaded.gold.conversations}
    split_entries = {item.conversation_id: item for item in loaded.split.entries}
    cases: dict[str, EvaluationCaseV3] = {}
    for binding in protocol.pilot_cases:
        gold_case = conversations.get(binding.case_id)
        split_entry = split_entries.get(binding.case_id)
        if gold_case is None or split_entry is None:
            raise ValueError("pilot case is absent from the loaded gold roster")
        if (
            split_entry.work_group_id != gold_case.work_group_id
            or split_entry.split.value != binding.split
            or split_entry.category != gold_case.category
        ):
            raise ValueError("pilot case binding differs from loaded evaluation gold")
        fixture_payload = (
            gold_case.sandbox_fixture.model_dump(mode="json")
            if gold_case.sandbox_fixture is not None
            else None
        )
        fixture = (
            SandboxFixtureAdapterV3(
                fixture_id=gold_case.sandbox_fixture.fixture_id,
                fixture_sha256=canonical_sha256(fixture_payload),
                reset_revision=gold_case.sandbox_fixture.reset_revision,
                payload=fixture_payload,
            )
            if fixture_payload is not None and gold_case.sandbox_fixture is not None
            else None
        )
        identity = gold_case.identity_fixture.model_dump(mode="json")
        cases[binding.case_id] = EvaluationCaseV3(
            case_id=gold_case.conversation_id,
            work_group_id=binding.work_group_id,
            category=gold_case.category.value,
            principal_role=gold_case.identity_fixture.role.value,
            scopes=tuple(scope.value for scope in gold_case.identity_fixture.scopes),
            resolved_product_ids=gold_case.product_ids,
            user_turns=tuple(
                EvaluationUserTurnV3(
                    source_turn_id=turn.turn_id,
                    ordinal=turn.ordinal,
                    message=turn.message,
                )
                for turn in gold_case.user_turns
            ),
            initial_state={"identity_fixture": identity},
            sandbox_fixture=fixture,
        )
    return cases


class EvaluationV3ObservationExecutorFactory:
    """Compose a fresh selected policy for every scheduled observation."""

    def __init__(
        self,
        shared_services: V2ServiceGraph,
        *,
        model_runtime: ModelRuntime,
    ) -> None:
        self.shared_services = shared_services
        self.composer = EvaluationV3VariantComposer(
            shared_services,
            model_runtime=model_runtime,
        )

    def __call__(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> EvaluationV3ObservationExecutor:
        runtime = self.composer.compose(context.identity.variant_id)
        return EvaluationV3ObservationExecutor(
            runtime=runtime,
            context=context,
            case=case,
        )


class EvaluationV3ObservationExecutor:
    """Reset one isolated namespace and execute its contiguous durable turns."""

    def __init__(
        self,
        *,
        runtime: EvaluationV3VariantRuntime,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> None:
        if runtime.policy.variant_id != context.identity.variant_id:
            raise ValueError("variant runtime and execution context differ")
        if case.case_id != context.identity.case_id:
            raise ValueError("evaluation case and execution context differ")
        self.runtime = runtime
        self.context = context
        self.case = case
        self.conversation_id = _compact_id(
            "conversation", context.namespace.conversation_id
        )
        self._reset = False
        self._next_ordinal = 1
        self._executed_ordinals: set[int] = set()

    async def reset_initial_state(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> InitialStateResetReceiptV3:
        if context != self.context or case != self.case:
            raise ObservationExecutionFailureV3("initial_state_reset_binding_invalid")
        self._reset_sql_state()
        self._reset = True
        self._next_ordinal = 1
        self._executed_ordinals.clear()
        return initial_state_reset_receipt_v3(context, case)

    async def execute_turn(
        self,
        request: UserTurnExecutionRequestV3,
    ) -> UserTurnExecutionResultV3:
        if not self._reset:
            raise ObservationExecutionFailureV3("initial_state_reset_required")
        if request.context != self.context or request.case_id != self.case.case_id:
            raise ObservationExecutionFailureV3("executor_request_binding_invalid")
        is_replay = request.user_turn.ordinal in self._executed_ordinals
        if not is_replay and request.user_turn.ordinal != self._next_ordinal:
            raise ObservationExecutionFailureV3("executor_turn_sequence_invalid")

        access = self._access()
        planning_context = PlanningContext(
            access=access,
            versions=self.runtime.shared_services.versions,
            resolved_product_ids=self.case.resolved_product_ids,
            merchant_target=self._merchant_target(),
        )
        durable_turn_id = canonical_turn_id(
            self.conversation_id,
            request.client_turn_id,
        )
        purpose: BudgetPurpose = (
            "warmup"
            if self.context.identity.turn_kind is ScheduledTurnKindV3.WARMUP
            else "benchmark"
        )
        limits = request.context.limits
        ledger = self.runtime.shared_services.budget_ledger
        ledger.create_scope(
            scope_id=durable_turn_id,
            account_id=self.runtime.shared_services.budget_account_id,
            purpose=purpose,
            hard_limit_nano_usd=DEFAULT_TURN_LIMIT_NANO_USD,
            max_generation_calls=DEFAULT_MAX_GENERATION_CALLS,
            max_provider_attempts=DEFAULT_MAX_PROVIDER_ATTEMPTS,
            max_concurrency=DEFAULT_MAX_CONCURRENCY,
        )
        budget = ProviderBudgetContext(
            ledger=ledger,
            scope_id=durable_turn_id,
            purpose=purpose,
            max_retries=DEFAULT_MAX_RETRIES,
            attempt_timeout_seconds=DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
        )
        if (
            limits.per_turn_limit_usd != Decimal("0.25")
            or limits.max_generation_calls != DEFAULT_MAX_GENERATION_CALLS
            or limits.max_provider_attempts != DEFAULT_MAX_PROVIDER_ATTEMPTS
            or limits.provider_concurrency != DEFAULT_MAX_CONCURRENCY
            or limits.max_retries != DEFAULT_MAX_RETRIES
            or limits.attempt_timeout_seconds != DEFAULT_ATTEMPT_TIMEOUT_SECONDS
            or limits.turn_deadline_seconds != 60.0
            or limits.max_input_tokens_per_generation != 12_000
            or limits.max_output_tokens_per_generation != 1_200
        ):
            raise ObservationExecutionFailureV3("executor_resource_limits_invalid")

        recorder_start = len(self.runtime.model_calls.snapshot())
        started = monotonic()
        outcome = None
        execution_error_code: str | None = None
        try:
            outcome = await self.runtime.turn_service.execute(
                ChatRequest(
                    conversation_id=self.conversation_id,
                    client_turn_id=request.client_turn_id,
                    message=request.user_turn.message,
                ),
                planning_context,
                provider_budget=budget,
            )
        except asyncio.CancelledError:
            execution_error_code = "turn_execution_cancelled"
        except Exception:
            execution_error_code = "observation_executor_failed"
        finally:
            elapsed = monotonic() - started
            attempts = ledger.scope_attempt_snapshots(durable_turn_id)
        recorded_calls = self.runtime.model_calls.snapshot()[recorder_start:]
        result = _turn_result(
            request=request,
            outcome_payload=(
                outcome.model_dump(mode="json")
                if outcome is not None
                else {
                    "turn_id": durable_turn_id,
                    "status": "failed",
                    "error": {
                        "code": execution_error_code or "observation_executor_failed"
                    },
                    "reused": False,
                }
            ),
            attempts=attempts,
            elapsed_seconds=elapsed,
        )
        if execution_error_code is not None or outcome is None:
            raise ObservationExecutionFailureV3(
                execution_error_code or "observation_executor_failed",
                partial_results=(result,),
            )
        if not self.runtime.policy.rag_enabled:
            if (
                outcome.usage.knowledge_retrievals != 0
                or _persisted_knowledge_retrievals(
                    self.runtime.shared_services.session_factory,
                    durable_turn_id,
                )
                != 0
                or result.embedding_calls
                or any(call.stage == "knowledge_query_plan" for call in recorded_calls)
            ):
                raise ObservationExecutionFailureV3(
                    "no_rag_postcondition_failed",
                    partial_results=(result,),
                )
        if not is_replay:
            self._executed_ordinals.add(request.user_turn.ordinal)
            self._next_ordinal += 1
        if outcome.status is not TurnStatus.COMPLETED:
            error_code = (
                outcome.error.code
                if outcome.error is not None
                else f"turn_{outcome.status.value}"
            )
            raise ObservationExecutionFailureV3(
                error_code,
                partial_results=(result,),
            )
        return result

    def _access(self) -> ResourceAuthorization:
        return ResourceAuthorization(
            binding=ResourceBinding(
                tenant_id=self.context.namespace.tenant_id,
                principal_id=self.context.namespace.principal_id,
                mode=ConversationMode(self.case.principal_role),
                store_id=_STORE_ID,
            ),
            scopes=frozenset(self.case.scopes),
        )

    def _reset_sql_state(self) -> None:
        services = self.runtime.shared_services
        binding = self._access().binding
        fixture = self.case.sandbox_fixture
        with services.session_factory() as session, session.begin():
            conversation = session.get(V2Conversation, self.conversation_id)
            if conversation is not None:
                _require_owner(
                    conversation, binding, "evaluation_conversation_collision"
                )
                proposal_ids = tuple(
                    session.scalars(
                        select(V2Proposal.id).where(
                            V2Proposal.conversation_id == self.conversation_id
                        )
                    )
                )
                if proposal_ids:
                    session.execute(
                        delete(V2ActionAudit).where(
                            V2ActionAudit.proposal_id.in_(proposal_ids)
                        )
                    )
                    session.execute(
                        delete(V2ActionIdempotency).where(
                            V2ActionIdempotency.proposal_id.in_(proposal_ids)
                        )
                    )
                session.execute(
                    delete(V2Proposal).where(
                        V2Proposal.conversation_id == self.conversation_id
                    )
                )
                turn_ids = tuple(
                    session.scalars(
                        select(V2Turn.id).where(
                            V2Turn.conversation_id == self.conversation_id
                        )
                    )
                )
                if turn_ids:
                    session.execute(
                        delete(V2Preference).where(
                            V2Preference.source_turn_id.in_(turn_ids)
                        )
                    )
                    session.execute(
                        delete(V2StepResult).where(V2StepResult.turn_id.in_(turn_ids))
                    )
                session.execute(
                    delete(V2Turn).where(V2Turn.conversation_id == self.conversation_id)
                )
                session.execute(
                    delete(V2Conversation).where(
                        V2Conversation.id == self.conversation_id
                    )
                )

            if fixture is not None:
                self._reset_fixture(session, fixture.payload, binding)
            session.add(
                V2Conversation(
                    id=self.conversation_id,
                    tenant_id=binding.tenant_id,
                    principal_id=binding.principal_id,
                    mode=binding.mode.value,
                    store_id=binding.store_id,
                    title=f"evaluation:{self.case.case_id}",
                )
            )

    def _reset_fixture(
        self,
        session: Any,
        payload: Mapping[str, Any],
        binding: ResourceBinding,
    ) -> None:
        cart_payload = payload.get("cart")
        merchant_payload = payload.get("merchant")
        offer_rows: list[tuple[str, int, int, int, int]] = []
        cart_id: str | None = None
        if isinstance(cart_payload, Mapping):
            source_cart_id = str(cart_payload["cart_id"])
            cart_id = self.fixture_resource_id("cart", source_cart_id)
            lines = cast(Sequence[Mapping[str, Any]], cart_payload["lines"])
            for line in lines:
                product_id = int(line["product_id"])
                price = int(line["unit_price_vnd"])
                quantity = int(line["quantity"])
                offer_rows.append(
                    (
                        self.fixture_resource_id("offer", str(product_id)),
                        product_id,
                        price,
                        max(quantity, 1),
                        1,
                    )
                )
        elif isinstance(merchant_payload, Mapping):
            for offer in cast(Sequence[Mapping[str, Any]], merchant_payload["offers"]):
                offer_rows.append(
                    (
                        self.fixture_resource_id("offer", str(offer["offer_id"])),
                        int(offer["product_id"]),
                        int(offer["price_vnd"]),
                        int(offer["available_quantity"]),
                        int(offer["version"]),
                    )
                )
        else:
            raise ObservationExecutionFailureV3("sandbox_fixture_state_invalid")

        if cart_id is not None:
            existing_cart = session.get(V2Cart, cart_id)
            if existing_cart is not None:
                _require_owner(existing_cart, binding, "evaluation_cart_collision")
                orders = tuple(
                    session.scalars(select(V2Order).where(V2Order.cart_id == cart_id))
                )
                if orders:
                    raise ObservationExecutionFailureV3("evaluation_order_collision")
                session.execute(delete(V2CartLine).where(V2CartLine.cart_id == cart_id))
                session.execute(delete(V2Cart).where(V2Cart.id == cart_id))

        for offer_id, *_ in offer_rows:
            existing_offer = session.get(V2Offer, offer_id)
            if existing_offer is not None:
                if (
                    existing_offer.tenant_id != binding.tenant_id
                    or existing_offer.store_id != binding.store_id
                ):
                    raise ObservationExecutionFailureV3("evaluation_offer_collision")
                session.execute(delete(V2Offer).where(V2Offer.id == offer_id))

        for offer_id, product_id, price, stock, version in offer_rows:
            if price <= 0:
                raise ObservationExecutionFailureV3("sandbox_fixture_price_invalid")
            session.add(
                V2Offer(
                    id=offer_id,
                    tenant_id=binding.tenant_id,
                    store_id=binding.store_id,
                    product_id=product_id,
                    demo_price_vnd=price,
                    stock=stock,
                    version=version,
                    is_active=True,
                )
            )
        session.flush()
        if cart_id is not None:
            assert isinstance(cart_payload, Mapping)
            session.add(
                V2Cart(
                    id=cart_id,
                    tenant_id=binding.tenant_id,
                    principal_id=binding.principal_id,
                    store_id=binding.store_id,
                    status="active",
                    version=int(cart_payload["version"]),
                )
            )
            session.flush()
            rows_by_product = {row[1]: row for row in offer_rows}
            for index, line in enumerate(
                cast(Sequence[Mapping[str, Any]], cart_payload["lines"]), start=1
            ):
                offer_id, product_id, _, _, version = rows_by_product[
                    int(line["product_id"])
                ]
                session.add(
                    V2CartLine(
                        id=self.fixture_resource_id("cartline", str(index)),
                        cart_id=cart_id,
                        tenant_id=binding.tenant_id,
                        principal_id=binding.principal_id,
                        store_id=binding.store_id,
                        offer_id=offer_id,
                        quantity=int(line["quantity"]),
                        offer_version=version,
                    )
                )

    def _merchant_target(self) -> ResolvedMerchantTarget | None:
        """Project only the server-owned target from the hashed sandbox fixture.

        Neither the expected response nor the proposed price enters planning;
        the planner must still parse the requested change from the user message.
        """
        adapter = self.case.sandbox_fixture
        if adapter is None or (
            adapter.payload.get("target_capability_id") != "merchant.offer.propose"
        ):
            return None
        fixture = SandboxFixtureV3.model_validate(adapter.payload)
        parameters = fixture.proposal_parameters
        if not isinstance(parameters, SandboxOfferProposalV3):
            return None
        assert fixture.merchant is not None
        offers = tuple(
            offer
            for offer in fixture.merchant.offers
            if offer.offer_id == parameters.offer_id
        )
        if (
            len(offers) != 1
            or offers[0].product_id not in self.case.resolved_product_ids
        ):
            raise ObservationExecutionFailureV3("merchant_target_binding_invalid")
        offer = offers[0]
        return ResolvedMerchantTarget(
            product_id=offer.product_id,
            offer_id=self.fixture_resource_id("offer", offer.offer_id),
            expected_version=offer.version,
        )

    def fixture_resource_id(self, kind: str, source_id: str) -> str:
        return _compact_id(
            kind,
            f"{self.context.namespace.namespace_id}:{source_id}",
        )


def _persisted_knowledge_retrievals(
    session_factory: Any,
    turn_id: str,
) -> int:
    with session_factory() as session:
        turn = session.get(V2Turn, turn_id)
        if turn is None:
            raise ObservationExecutionFailureV3("durable_turn_evidence_missing")
        result = turn.result
    if result is None:
        return 0
    if not isinstance(result, dict):
        raise ObservationExecutionFailureV3("durable_turn_evidence_invalid")
    runtime = result.get("runtime")
    if not isinstance(runtime, dict):
        raise ObservationExecutionFailureV3("durable_turn_evidence_invalid")
    count = runtime.get("knowledge_retrievals")
    if type(count) is not int or not 0 <= count <= MAX_KNOWLEDGE_RETRIEVALS:
        raise ObservationExecutionFailureV3("durable_turn_evidence_invalid")
    return count


def _turn_result(
    *,
    request: UserTurnExecutionRequestV3,
    outcome_payload: dict[str, Any],
    attempts: Sequence[ProviderAttemptSnapshot],
    elapsed_seconds: float,
) -> UserTurnExecutionResultV3:
    _validate_attempt_rows(attempts)
    grouped: dict[str, list[ProviderAttemptSnapshot]] = defaultdict(list)
    dispatched_attempts = tuple(
        attempt for attempt in attempts if attempt.transport_started_at is not None
    )
    for attempt in dispatched_attempts:
        grouped[attempt.call_id].append(attempt)
    model_calls: list[ModelCallEvidenceV3] = []
    embedding_calls: list[EmbeddingCallEvidenceV3] = []
    retry_events: list[RetryEvidenceV3] = []
    for call_id, rows in grouped.items():
        first = rows[0]
        for retry_ordinal in range(1, len(rows)):
            retry_events.append(
                RetryEvidenceV3(
                    retry_event_id=_evidence_id(
                        "retry",
                        request.attribution.execution_turn_id,
                        call_id,
                        retry_ordinal,
                    ),
                    attribution=request.attribution,
                    call_id=call_id,
                    retry_ordinal=retry_ordinal,
                )
            )
        if first.operation == "generation":
            model_calls.append(
                ModelCallEvidenceV3(
                    call_id=call_id,
                    attribution=request.attribution,
                    model=first.resolved_model,
                    attempts=len(rows),
                    input_tokens=sum(row.input_tokens or 0 for row in rows),
                    cached_input_tokens=sum(
                        row.cached_input_tokens or 0 for row in rows
                    ),
                    output_tokens=sum(row.output_tokens or 0 for row in rows),
                    reasoning_tokens=sum(row.reasoning_tokens or 0 for row in rows),
                    total_tokens=sum(row.total_tokens or 0 for row in rows),
                )
            )
        else:
            embedding_calls.append(
                EmbeddingCallEvidenceV3(
                    call_id=call_id,
                    attribution=request.attribution,
                    model=first.resolved_model,
                    attempts=len(rows),
                    input_tokens=sum(row.input_tokens or 0 for row in rows),
                )
            )

    ledger_events = tuple(_ledger_event(request, row) for row in attempts)
    if not ledger_events:
        ledger_events = (
            LedgerEventEvidenceV3(
                ledger_event_id=_evidence_id(
                    "ledger", request.attribution.execution_turn_id, "empty"
                ),
                attribution=request.attribution,
                kind=LedgerEventKindV3.ZERO_COST,
            ),
        )
    return UserTurnExecutionResultV3(
        attribution=request.attribution,
        result_payload=outcome_payload,
        model_calls=tuple(model_calls),
        embedding_calls=tuple(embedding_calls),
        retry_events=tuple(retry_events),
        ledger_events=ledger_events,
        peak_provider_concurrency=_peak_concurrency(dispatched_attempts),
        max_attempt_duration_seconds=_max_attempt_duration(dispatched_attempts),
        elapsed_seconds=elapsed_seconds,
    )


def _validate_attempt_rows(attempts: Sequence[ProviderAttemptSnapshot]) -> None:
    if tuple(row.attempt_sequence for row in attempts) != tuple(
        range(1, len(attempts) + 1)
    ):
        raise ObservationExecutionFailureV3("ledger_attempt_sequence_invalid")
    grouped: dict[str, list[ProviderAttemptSnapshot]] = defaultdict(list)
    for row in attempts:
        grouped[row.call_id].append(row)
        if row.usage_status == "known":
            usage = (
                row.input_tokens,
                row.cached_input_tokens,
                row.output_tokens,
                row.reasoning_tokens,
                row.total_tokens,
                row.actual_cost_nano_usd,
            )
            if any(value is None for value in usage):
                raise ObservationExecutionFailureV3("ledger_known_usage_incomplete")
        elif row.actual_cost_nano_usd is not None:
            raise ObservationExecutionFailureV3("ledger_unresolved_cost_ambiguous")
    for rows in grouped.values():
        if tuple(row.attempt_number for row in rows) != tuple(range(1, len(rows) + 1)):
            raise ObservationExecutionFailureV3("ledger_retry_sequence_invalid")
        identity = {(row.operation, row.resolved_model) for row in rows}
        if len(identity) != 1:
            raise ObservationExecutionFailureV3("ledger_retry_identity_invalid")


def _ledger_event(
    request: UserTurnExecutionRequestV3,
    row: ProviderAttemptSnapshot,
) -> LedgerEventEvidenceV3:
    if row.usage_status == "known":
        actual = row.actual_cost_nano_usd
        if actual is None:
            raise ObservationExecutionFailureV3("ledger_known_cost_missing")
        if actual > 0:
            kind = LedgerEventKindV3.SETTLED_KNOWN
        elif row.transport_started_at is None:
            kind = LedgerEventKindV3.RELEASED
        else:
            kind = LedgerEventKindV3.ZERO_COST
        return LedgerEventEvidenceV3(
            ledger_event_id=_evidence_id(
                "ledger", request.attribution.execution_turn_id, row.attempt_id
            ),
            attribution=request.attribution,
            kind=kind,
            reservation_id=row.attempt_id,
            known_cost_usd=nano_usd_to_usd(actual),
        )
    return LedgerEventEvidenceV3(
        ledger_event_id=_evidence_id(
            "ledger", request.attribution.execution_turn_id, row.attempt_id
        ),
        attribution=request.attribution,
        kind=LedgerEventKindV3.UNRESOLVED_RESERVATION,
        reservation_id=row.attempt_id,
        unresolved_reserved_maximum_usd=nano_usd_to_usd(row.reserved_nano_usd),
    )


def _peak_concurrency(attempts: Sequence[ProviderAttemptSnapshot]) -> int:
    events: list[tuple[datetime, int]] = []
    for row in attempts:
        if row.transport_started_at is None:
            continue
        end = row.settled_at or row.result_recorded_at or row.transport_started_at
        events.append((row.transport_started_at, 1))
        events.append((end, -1))
    active = peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], -item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def _max_attempt_duration(attempts: Sequence[ProviderAttemptSnapshot]) -> float:
    durations: list[float] = []
    for row in attempts:
        started = row.transport_started_at
        ended = row.settled_at or row.result_recorded_at
        if started is not None and ended is not None:
            durations.append((ended - started).total_seconds())
    return max((max(value, 0.0) for value in durations), default=0.0)


def _require_owner(resource: Any, binding: ResourceBinding, code: str) -> None:
    actual = (
        str(resource.tenant_id),
        str(resource.principal_id),
        str(resource.store_id),
    )
    expected = (binding.tenant_id, binding.principal_id, binding.store_id)
    if actual != expected:
        raise ObservationExecutionFailureV3(code)
    resource_mode = getattr(resource, "mode", binding.mode.value)
    if str(resource_mode) != binding.mode.value:
        raise ObservationExecutionFailureV3(code)


def _compact_id(prefix: str, material: str) -> str:
    return f"{prefix}_{sha256(material.encode('utf-8')).hexdigest()[:48]}"


def _evidence_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{canonical_sha256([str(part) for part in parts])}"


__all__ = [
    "EvaluationV3ObservationExecutor",
    "EvaluationV3ObservationExecutorFactory",
    "build_evaluation_cases_v3",
]
