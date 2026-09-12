"""Positive and negative coverage for strict API v2 contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts import TaskStatus
from app.v2.contracts import (
    MAX_MESSAGE_LENGTH,
    ActionCard,
    ActionChange,
    ActionConfirmRequest,
    ActionDecisionResponse,
    ActionExecutionResponse,
    ActionKind,
    ActionReadResponse,
    ActionStatus,
    ActionTarget,
    BudgetPreference,
    ChatRequest,
    Citation,
    Claim,
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationMode,
    ConversationSummary,
    DialogueOutcome,
    EvidenceKind,
    EvidenceReference,
    ExecutionRecord,
    GenrePreference,
    HistoryTurn,
    PlanRevision,
    PlanStep,
    PlanTrace,
    PreferenceKind,
    PreferencePutRequest,
    SafeExecutionError,
    TurnResponse,
    TurnResult,
    TurnStatus,
    TurnSummary,
    UsageSummary,
)

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def _step(
    suffix: str,
    capability: str = "product.catalog.search",
    *,
    depends_on: tuple[str, ...] = (),
) -> PlanStep:
    return PlanStep(
        step_id=f"step_{suffix}",
        operation_key=f"operation-key-{suffix}",
        capability=capability,
        service="product" if capability != "knowledge.retrieve" else "knowledge",
        depends_on=depends_on,
        data_version_ids=("version_catalog_001",),
    )


def _evidence(suffix: str = "001", *, display_label: str = "[C1]") -> EvidenceReference:
    return EvidenceReference(
        evidence_id=f"evidence_{suffix}",
        source_id=f"source_{suffix}",
        source_version_id=f"version_{suffix}",
        chunk_id=f"chunk_{suffix}",
        span_id=f"span_{suffix}",
        display_label=display_label,
        kind=EvidenceKind.KNOWLEDGE,
        title="Nguồn kiểm chứng",
        url="https://example.test/books/1",
        observed_at=NOW,
    )


def _action_card(
    *,
    status: ActionStatus = ActionStatus.PROPOSED,
    action_id: str = "action_00000001",
) -> ActionCard:
    return ActionCard(
        action_id=action_id,
        proposal_id=action_id,
        proposal_version=1,
        kind=ActionKind.CHECKOUT,
        status=status,
        title="Xác nhận đơn hàng sandbox",
        required_permission="ecommerce.write",
        confirmation_required=True,
        target=ActionTarget(
            resource_type="cart",
            resource_id="cart_00000001",
            expected_resource_version=3,
            data_version_ids=("snapshot_00000001",),
        ),
        changes=(
            ActionChange(
                resource_type="order",
                resource_id="order_00000001",
                field="status",
                before_text="cart_active",
                after_text="confirmed",
            ),
        ),
        expires_at=NOW + timedelta(minutes=10),
    )


def test_chat_request_is_strict_and_serializable() -> None:
    request = ChatRequest(
        conversation_id="conversation_001",
        client_turn_id="browser:turn-1",
        message="Tìm sách lịch sử dưới 200.000 đồng",
    )

    assert ChatRequest.model_validate_json(request.model_dump_json()) == request
    assert set(request.model_dump()) == {
        "conversation_id",
        "client_turn_id",
        "message",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "conversation_id": "conversation_001",
            "client_turn_id": "turn-1",
            "message": " ",
        },
        {
            "conversation_id": "conversation_001",
            "client_turn_id": "turn-1",
            "message": "x" * (MAX_MESSAGE_LENGTH + 1),
        },
        {
            "conversation_id": "conversation_001",
            "client_turn_id": "turn-1",
            "message": "hello",
            "tenant_id": "tenant-a",
        },
        {
            "conversation_id": "conversation_001",
            "client_turn_id": "turn-1",
            "message": "hello",
            "role": "merchant",
        },
        {
            "conversation_id": "conversation_001",
            "client_turn_id": "turn-1",
            "message": "hello",
            "store_id": "foreign-store",
        },
    ],
)
def test_chat_request_rejects_bounds_and_authority_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


def test_conversation_create_accepts_only_mode() -> None:
    assert ConversationCreateRequest(mode="shopper").mode == ConversationMode.SHOPPER

    for authority_field in ("principal_id", "tenant_id", "role", "store_id"):
        with pytest.raises(ValidationError):
            ConversationCreateRequest.model_validate(
                {"mode": "shopper", authority_field: "attacker"}
            )


def test_task_status_stays_frozen_and_turn_lifecycle_is_separate() -> None:
    assert tuple(status.value for status in TaskStatus) == (
        "pending",
        "running",
        "success",
        "partial_success",
        "failed",
    )
    assert TurnStatus.CANCELLED.value == "cancelled"
    assert TurnStatus.INTERRUPTED.value == "interrupted"
    assert "cancelled" not in {status.value for status in TaskStatus}


def test_plan_trace_records_one_continuation_and_reuses_completed_steps() -> None:
    product = _step("product")
    knowledge = _step(
        "knowledge",
        "knowledge.retrieve",
        depends_on=(product.step_id,),
    )
    trace = PlanTrace(
        plan_id="plan_00000001",
        intent="catalog_knowledge",
        revisions=(
            PlanRevision(
                revision=0,
                reason="initial plan",
                steps=(product,),
                executed_step_ids=(product.step_id,),
            ),
            PlanRevision(
                revision=1,
                reason="knowledge evidence was missing",
                steps=(product, knowledge),
                added_read_step_ids=(knowledge.step_id,),
                executed_step_ids=(knowledge.step_id,),
                reused_step_ids=(product.step_id,),
            ),
        ),
    )

    assert trace.revisions[1].reused_step_ids == ("step_product",)
    assert len(trace.revisions) == 2


def test_plan_trace_rejects_mutated_reuse_and_duplicate_dispatch() -> None:
    product = _step("product")
    changed = product.model_copy(update={"capability": "product.rank"})

    with pytest.raises(ValidationError, match="cannot change its definition"):
        PlanTrace(
            plan_id="plan_00000001",
            intent="recommendation",
            revisions=(
                PlanRevision(
                    revision=0,
                    reason="initial",
                    steps=(product,),
                    executed_step_ids=(product.step_id,),
                ),
                PlanRevision(
                    revision=1,
                    reason="changed",
                    steps=(changed,),
                    reused_step_ids=(changed.step_id,),
                ),
            ),
        )

    with pytest.raises(ValidationError, match="dispatched twice"):
        PlanTrace(
            plan_id="plan_00000002",
            intent="catalog",
            revisions=(
                PlanRevision(
                    revision=0,
                    reason="initial",
                    steps=(product,),
                    executed_step_ids=(product.step_id,),
                ),
                PlanRevision(
                    revision=1,
                    reason="retry",
                    steps=(product,),
                    executed_step_ids=(product.step_id,),
                ),
            ),
        )


def test_plan_trace_enforces_step_and_knowledge_limits() -> None:
    nine_steps = tuple(_step(f"read_{index}") for index in range(9))
    with pytest.raises(ValidationError):
        PlanRevision(revision=0, reason="too many", steps=nine_steps)

    knowledge_steps = tuple(
        _step(f"knowledge_{index}", "knowledge.retrieve") for index in range(3)
    )
    with pytest.raises(ValidationError, match="knowledge retrieval limit"):
        PlanTrace(
            plan_id="plan_knowledge",
            intent="knowledge",
            revisions=(
                PlanRevision(
                    revision=0,
                    reason="too many retrievals",
                    steps=knowledge_steps,
                ),
            ),
        )


def test_continuation_cannot_hide_new_steps_from_added_read_ids() -> None:
    product = _step("product")
    added = tuple(_step(f"extra_{index}") for index in range(3))

    with pytest.raises(ValidationError, match="exactly match added read"):
        PlanTrace(
            plan_id="plan_hidden_continuation",
            intent="catalog",
            revisions=(
                PlanRevision(
                    revision=0,
                    reason="initial",
                    steps=(product,),
                    executed_step_ids=(product.step_id,),
                ),
                PlanRevision(
                    revision=1,
                    reason="hidden additions",
                    steps=(product, *added),
                    reused_step_ids=(product.step_id,),
                ),
            ),
        )


def test_grounded_turn_serializes_stable_evidence_references() -> None:
    evidence = _evidence()
    citation = Citation(
        citation_id="citation_001",
        claim_id="claim_001",
        evidence_id=evidence.evidence_id,
        span_id=evidence.span_id,
        display_label=evidence.display_label,
    )
    claim = Claim(
        claim_id="claim_001",
        text="Sách có nguồn mô tả nội dung.",
        citation_ids=(citation.citation_id,),
    )
    turn = TurnSummary(
        turn_id="turn_00000001",
        client_turn_id="client-turn-1",
        status=TurnStatus.COMPLETED,
        outcome=DialogueOutcome.ANSWERED,
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )
    response = TurnResponse(
        conversation_id="conversation_001",
        turn=turn,
        request_id="request_001",
        trace_id="trace_001",
        result=TurnResult(
            outcome=DialogueOutcome.ANSWERED,
            answer="Đây là câu trả lời có kiểm chứng [C1].",
            claims=(claim,),
            citations=(citation,),
            evidence=(evidence,),
            executions=(
                ExecutionRecord(
                    execution_id="execution_001",
                    step_id="step_product",
                    operation_key="operation-key-product",
                    capability="product.catalog.search",
                    service="product",
                    status=TaskStatus.SUCCESS,
                ),
            ),
        ),
        usage=UsageSummary(
            input_tokens=20,
            cached_input_tokens=5,
            output_tokens=10,
            reasoning_tokens=2,
            total_tokens=30,
            generation_calls=1,
            provider_attempts=1,
            estimated_cost_usd=Decimal("0.001"),
            known_cost_usd=Decimal("0.001"),
        ),
    )

    restored = TurnResponse.model_validate_json(response.model_dump_json())
    assert restored == response
    assert restored.result is not None
    assert restored.result.evidence[0].display_label == "[C1]"
    assert restored.result.evidence[0].evidence_id == "evidence_001"


def test_claim_cannot_borrow_another_claims_citation() -> None:
    first = _evidence("001", display_label="[C1]")
    second = _evidence("002", display_label="[C2]")
    citations = (
        Citation(
            citation_id="citation_001",
            claim_id="claim_001",
            evidence_id=first.evidence_id,
            span_id=first.span_id,
            display_label=first.display_label,
        ),
        Citation(
            citation_id="citation_002",
            claim_id="claim_002",
            evidence_id=second.evidence_id,
            span_id=second.span_id,
            display_label=second.display_label,
        ),
    )

    with pytest.raises(ValidationError, match="owned by another claim"):
        TurnResult(
            outcome=DialogueOutcome.ANSWERED,
            answer="Unsupported citation ownership",
            claims=(
                Claim(
                    claim_id="claim_001",
                    text="Claim one",
                    citation_ids=("citation_002",),
                ),
                Claim(
                    claim_id="claim_002",
                    text="Claim two",
                    citation_ids=("citation_001",),
                ),
            ),
            citations=citations,
            evidence=(first, second),
        )


def test_citation_must_appear_in_its_owner_claim() -> None:
    first = _evidence("001", display_label="[C1]")
    second = _evidence("002", display_label="[C2]")
    citations = (
        Citation(
            citation_id="citation_001",
            claim_id="claim_001",
            evidence_id=first.evidence_id,
            span_id=first.span_id,
            display_label=first.display_label,
        ),
        Citation(
            citation_id="citation_002",
            claim_id="claim_001",
            evidence_id=second.evidence_id,
            span_id=second.span_id,
            display_label=second.display_label,
        ),
    )

    with pytest.raises(ValidationError, match="absent from its owning claim"):
        TurnResult(
            outcome=DialogueOutcome.ANSWERED,
            answer="A citation was omitted from the owner claim",
            claims=(
                Claim(
                    claim_id="claim_001",
                    text="Claim one",
                    citation_ids=("citation_001",),
                ),
            ),
            citations=citations,
            evidence=(first, second),
        )


def test_evidence_display_labels_are_unique_and_separate_from_stable_ids() -> None:
    with pytest.raises(ValidationError, match="display labels must be unique"):
        TurnResult(
            outcome=DialogueOutcome.ANSWERED,
            answer="Duplicate labels",
            evidence=(
                _evidence("001", display_label="[C1]"),
                _evidence("002", display_label="[C1]"),
            ),
        )


def test_action_card_requires_target_version_and_confirmation() -> None:
    target = ActionTarget(
        resource_type="offer",
        resource_id="offer_00000001",
        expected_resource_version=3,
        data_version_ids=("snapshot_00000001",),
    )
    change = ActionChange(
        resource_type="offer",
        resource_id="offer_00000001",
        field="price_vnd",
        before_integer=120_000,
        after_integer=110_000,
    )
    card = ActionCard(
        action_id="action_00000001",
        proposal_id="proposal_00000001",
        proposal_version=1,
        kind=ActionKind.MERCHANT_PRICE_CHANGE,
        status=ActionStatus.PROPOSED,
        title="Đổi giá offer demo",
        required_permission="merchant.write",
        confirmation_required=True,
        target=target,
        changes=(change,),
        expires_at=NOW + timedelta(minutes=10),
    )

    assert card.target.expected_resource_version == 3
    assert card.proposal_version == 1
    with pytest.raises(ValidationError, match="requires explicit confirmation"):
        ActionCard.model_validate(card.model_dump() | {"confirmation_required": False})


@pytest.mark.parametrize("version", [0, -1, "1", 1.5])
def test_action_confirmation_rejects_invalid_versions(version: object) -> None:
    with pytest.raises(ValidationError):
        ActionConfirmRequest.model_validate({"proposal_version": version})


def test_action_confirmation_keeps_idempotency_in_the_header_contract() -> None:
    request = ActionConfirmRequest(proposal_version=2)

    assert request.model_dump() == {"proposal_version": 2}
    with pytest.raises(ValidationError):
        ActionConfirmRequest.model_validate(
            {"proposal_version": 2, "idempotency_key": "header-value"}
        )


def test_conversation_detail_uses_the_rich_history_turn_contract() -> None:
    evidence = _evidence()
    citation = Citation(
        citation_id="citation_history_001",
        claim_id="claim_history_001",
        evidence_id=evidence.evidence_id,
        span_id=evidence.span_id,
        display_label=evidence.display_label,
    )
    claim = Claim(
        claim_id="claim_history_001",
        text="Đơn sandbox cần được xác nhận.",
        citation_ids=(citation.citation_id,),
    )
    card = _action_card()
    result = TurnResult(
        outcome=DialogueOutcome.AWAITING_CONFIRMATION,
        answer="Vui lòng xác nhận đơn hàng [C1].",
        claims=(claim,),
        citations=(citation,),
        evidence=(evidence,),
        action_cards=(card,),
    )
    history_turn = HistoryTurn(
        turn_id="turn_history_001",
        client_turn_id="client-history-1",
        status=TurnStatus.COMPLETED,
        outcome=result.outcome,
        user_message="Tạo đơn hàng sandbox",
        assistant_result=result,
        action_cards=result.action_cards,
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )
    detail = ConversationDetailResponse(
        conversation=ConversationSummary(
            conversation_id="conversation_history_001",
            mode=ConversationMode.SHOPPER,
            store_id="store_001",
            created_at=NOW,
            updated_at=NOW + timedelta(seconds=1),
        ),
        turns=(history_turn,),
    )

    restored = ConversationDetailResponse.model_validate_json(detail.model_dump_json())
    assert restored.turns[0].user_message == "Tạo đơn hàng sandbox"
    assert restored.turns[0].assistant_result == result
    definitions = ConversationDetailResponse.model_json_schema()["$defs"]
    assert "HistoryTurn" in definitions
    assert "TurnSummary" not in definitions
    with pytest.raises(ValidationError):
        HistoryTurn.model_validate(
            history_turn.model_dump() | {"tenant_id": "attacker"}
        )


def test_action_read_response_unwraps_only_validated_public_results() -> None:
    assert ActionReadResponse(action=_action_card()).result is None
    action = _action_card(status=ActionStatus.EXECUTED)
    persisted = {
        "schema_version": 1,
        "response": {
            "action_id": action.action_id,
            "status": "executed",
            "resource_id": "order_00000001",
            "resource_version": 1,
            "reused_result": False,
        },
    }

    response = ActionReadResponse.from_persistence(
        action=action,
        persisted_result=persisted,
    )
    assert isinstance(response.result, ActionExecutionResponse)
    assert set(response.model_dump()) == {"action", "result"}
    serialized = response.model_dump_json()
    assert "schema_version" not in serialized
    assert "tenant_id" not in serialized

    rejected = _action_card(
        status=ActionStatus.REJECTED,
        action_id="action_rejected_001",
    )
    rejected_response = ActionReadResponse.from_persistence(
        action=rejected,
        persisted_result={
            "schema_version": 1,
            "response": {
                "action_id": rejected.action_id,
                "proposal_version": 1,
                "status": "rejected",
                "decided_at": NOW.isoformat(),
                "reused_result": False,
            },
            "reason": "Không mua nữa",
        },
    )
    assert isinstance(rejected_response.result, ActionDecisionResponse)
    assert "Không mua nữa" not in rejected_response.model_dump_json()

    deleted = _action_card(
        status=ActionStatus.EXPIRED,
        action_id="action_deleted_001",
    )
    assert (
        ActionReadResponse.from_persistence(
            action=deleted,
            persisted_result={"code": "conversation_deleted"},
        ).result
        is None
    )


@pytest.mark.parametrize(
    "persisted_result",
    [
        {
            "schema_version": 1,
            "response": {
                "action_id": "action_00000001",
                "status": "executed",
                "resource_id": "order_00000001",
                "resource_version": 1,
            },
            "tenant_id": "attacker",
        },
        {
            "schema_version": 1,
            "response": {
                "action_id": "action_00000001",
                "status": "executed",
                "resource_id": "order_00000001",
                "resource_version": 1,
                "principal_id": "attacker",
            },
        },
        {
            "schema_version": 1,
            "response": {
                "action_id": "action_00000001",
                "status": "executed",
                "resource_id": "order_00000001",
                "resource_version": 1,
            },
            "raw_tool_payload": {"secret": "do-not-expose"},
        },
        {
            "schema_version": 2,
            "response": {
                "action_id": "action_00000001",
                "status": "executed",
                "resource_id": "order_00000001",
                "resource_version": 1,
            },
        },
        {
            "schema_version": 1,
            "response": {
                "action_id": "action_different_001",
                "status": "executed",
                "resource_id": "order_00000001",
                "resource_version": 1,
            },
        },
    ],
)
def test_action_read_response_fails_closed_on_storage_or_authority_fields(
    persisted_result: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ActionReadResponse.from_persistence(
            action=_action_card(status=ActionStatus.EXECUTED),
            persisted_result=persisted_result,
        )


def test_usage_rejects_inconsistent_or_hidden_reservations() -> None:
    with pytest.raises(ValidationError, match="reasoning tokens"):
        UsageSummary(output_tokens=1, reasoning_tokens=2, total_tokens=1)
    with pytest.raises(ValidationError, match="estimated cost"):
        UsageSummary(
            reserved_cost_usd=Decimal("0.01"),
            unknown_reserved_cost_usd=Decimal("0.02"),
        )


def test_failed_and_interrupted_turns_expose_safe_error_and_usage() -> None:
    failed_turn = TurnSummary(
        turn_id="turn_failed_001",
        client_turn_id="client-failed-1",
        status=TurnStatus.FAILED,
        created_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )
    error = SafeExecutionError(
        code="provider.timeout",
        message="The model attempt timed out.",
        retryable=True,
    )
    usage = UsageSummary(
        provider_attempts=1,
        unknown_usage_attempts=1,
        estimated_cost_usd=Decimal("0.01"),
        unknown_reserved_cost_usd=Decimal("0.01"),
    )

    response = TurnResponse(
        conversation_id="conversation_001",
        turn=failed_turn,
        request_id="request_failed_001",
        trace_id="trace_failed_001",
        error=error,
        usage=usage,
    )
    assert response.usage.unknown_reserved_cost_usd == Decimal("0.01")
    assert response.error == error

    interrupted = failed_turn.model_copy(
        update={"turn_id": "turn_interrupted_001", "status": TurnStatus.INTERRUPTED}
    )
    with pytest.raises(ValidationError, match="require a safe error"):
        TurnResponse(
            conversation_id="conversation_001",
            turn=interrupted,
            request_id="request_interrupted_001",
            trace_id="trace_interrupted_001",
            usage=usage,
        )


def test_only_allowlisted_explicit_preferences_validate() -> None:
    genre = PreferencePutRequest(
        source_turn_id="turn_00000001",
        preference=GenrePreference(kind=PreferenceKind.GENRE, value="Lịch sử"),
    )
    budget = PreferencePutRequest(
        source_turn_id="turn_00000001",
        preference=BudgetPreference(
            kind=PreferenceKind.MAX_BUDGET_VND,
            value=200_000,
        ),
    )

    assert genre.preference.kind == PreferenceKind.GENRE
    assert budget.preference.value == 200_000
    with pytest.raises(ValidationError):
        PreferencePutRequest.model_validate(
            {
                "source_turn_id": "turn_00000001",
                "preference": {"kind": "political_belief", "value": "x"},
            }
        )
    with pytest.raises(ValidationError):
        PreferencePutRequest.model_validate(
            {
                "source_turn_id": "turn_00000001",
                "preference": {
                    "kind": "genre",
                    "value": "History",
                    "inferred": True,
                },
            }
        )


def test_public_contract_schemas_do_not_expose_sensitive_runtime_payloads() -> None:
    schemas = (
        ActionReadResponse.model_json_schema(),
        ConversationDetailResponse.model_json_schema(),
        TurnResponse.model_json_schema(),
        TurnResult.model_json_schema(),
        PlanTrace.model_json_schema(),
    )
    serialized = " ".join(str(schema).lower() for schema in schemas)

    for forbidden_name in (
        "raw_prompt",
        "chain_of_thought",
        "reasoning_trace",
        "raw_tool_payload",
    ):
        assert forbidden_name not in serialized
