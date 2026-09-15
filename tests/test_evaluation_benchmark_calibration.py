"""Focused Package 8 calibration bridge contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation import v3_cli
from app.evaluation.benchmark_calibration import (
    BenchmarkCalibrationValidationErrorV3,
    _exact_citations,
    build_calibration_label_policy_v3,
    build_package8_pilot_calibration_inputs_v3,
)
from app.evaluation.benchmark_reporting import (
    EvidenceBindingKeyV3,
    ResolvedExactEvidenceV3,
)
from app.evaluation.v3_judge import SEMANTIC_JUDGE_METRICS_V3
from app.evaluation.v3_models import PilotTurnCostV3, ScheduledTurnKindV3
from app.evaluation.v3_protocol import evaluation_protocol_sha256_v3
from app.evaluation.v3_runner import (
    EmbeddingCallEvidenceV3,
    EvaluationResourceLimitsV3,
    ExecutionAttributionV3,
    InitialStateResetReceiptV3,
    LedgerEventEvidenceV3,
    LedgerEventKindV3,
    ModelCallEvidenceV3,
    ObservationResourceTotalsV3,
    ObservationRunReceiptV3,
    ObservationTerminalStatusV3,
    UserTurnExecutionResultV3,
    execution_namespace_v3,
)
from app.evaluation.v3_schedule import build_pilot_schedule_v3, pilot_schedule_sha256_v3
from app.v2.contracts import (
    Citation,
    Claim,
    DialogueOutcome,
    EvidenceKind,
    EvidenceReference,
    TurnResult,
    TurnStatus,
)
from app.v2.execution import DurableTurnOutcome

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _policy():
    return build_calibration_label_policy_v3(
        maximum_absolute_error={metric: 0.25 for metric in SEMANTIC_JUDGE_METRICS_V3}
    )


class _Resolver:
    def __init__(self, values: dict[EvidenceBindingKeyV3, str] | None = None) -> None:
        self.values = values or {}

    def resolve(self, binding: EvidenceBindingKeyV3) -> ResolvedExactEvidenceV3 | None:
        value = self.values.get(binding)
        if value is None:
            return None
        return ResolvedExactEvidenceV3(binding=binding, exact_text=value)


@pytest.fixture(scope="module")
def package7_inputs():
    arguments = v3_cli._parser().parse_args(
        ("validate", "--project-root", str(PROJECT_ROOT))
    )
    return v3_cli._load_inputs(arguments)


def _synthetic_receipts(package7_inputs, run_id: str = "run_p8_calibration"):
    protocol = package7_inputs.protocol
    protocol_sha256 = evaluation_protocol_sha256_v3(protocol)
    schedule = build_pilot_schedule_v3(
        protocol,
        run_id=run_id,
        protocol_sha256=protocol_sha256,
    )
    schedule_sha256 = pilot_schedule_sha256_v3(schedule)
    execution_case_set_sha256 = "c" * 64
    receipts: list[ObservationRunReceiptV3] = []
    for turn in schedule:
        execution_case_sha256 = "d" * 64
        namespace = execution_namespace_v3(
            run_id=run_id,
            protocol_sha256=protocol_sha256,
            execution_case_sha256=execution_case_sha256,
            execution_case_set_sha256=execution_case_set_sha256,
            schedule_sha256=schedule_sha256,
            canonical_turn_id=turn.turn_id,
            observation_id=turn.observation_id,
        )
        attribution = ExecutionAttributionV3(
            run_id=run_id,
            canonical_turn_id=turn.turn_id,
            observation_id=turn.observation_id,
            execution_turn_id=f"eturn_{turn.schedule_index:064x}",
            source_turn_id=f"source_{turn.schedule_index:03d}",
        )
        result = TurnResult(
            outcome=DialogueOutcome.ANSWERED,
            answer="Synthetic development pilot answer.",
        )
        durable = DurableTurnOutcome(
            turn_id=f"durable_{turn.schedule_index:03d}",
            status=TurnStatus.COMPLETED,
            outcome=result.outcome,
            result=result,
        )
        model_call = ModelCallEvidenceV3(
            call_id=f"mcall_{turn.schedule_index:03d}",
            attribution=attribution,
            model="synthetic-model",
            attempts=1,
            input_tokens=10,
            cached_input_tokens=0,
            output_tokens=5,
            reasoning_tokens=0,
            total_tokens=15,
        )
        embedding_call = EmbeddingCallEvidenceV3(
            call_id=f"ecall_{turn.schedule_index:03d}",
            attribution=attribution,
            model="synthetic-embedding",
            attempts=1,
            input_tokens=3,
        )
        ledger_event = LedgerEventEvidenceV3(
            ledger_event_id=f"ledger_{turn.schedule_index:03d}",
            attribution=attribution,
            kind=LedgerEventKindV3.SETTLED_KNOWN,
            known_cost_usd=Decimal("0.01"),
        )
        turn_result = UserTurnExecutionResultV3(
            attribution=attribution,
            result_payload=durable.model_dump(mode="json"),
            model_calls=(model_call,),
            embedding_calls=(embedding_call,),
            ledger_events=(ledger_event,),
            peak_provider_concurrency=1,
            max_attempt_duration_seconds=0.1,
            elapsed_seconds=0.2,
        )
        totals = ObservationResourceTotalsV3(
            generation_calls=1,
            provider_attempts=2,
            retry_count=0,
            peak_provider_concurrency=1,
            input_tokens=13,
            output_tokens=5,
            known_cost_usd=Decimal("0.01"),
            unresolved_reserved_cost_usd=Decimal("0"),
        )
        reset = InitialStateResetReceiptV3(
            reset_id=f"reset_{turn.schedule_index:064x}",
            run_id=run_id,
            execution_case_sha256=execution_case_sha256,
            execution_case_set_sha256=execution_case_set_sha256,
            canonical_turn_id=turn.turn_id,
            observation_id=turn.observation_id,
            namespace_id=namespace.namespace_id,
            case_id=turn.identity.case_id,
            work_group_id=turn.identity.work_group_id,
            initial_state_sha256="e" * 64,
        )
        receipts.append(
            ObservationRunReceiptV3(
                terminal_status=ObservationTerminalStatusV3.COMPLETED,
                run_id=run_id,
                protocol_sha256=protocol_sha256,
                execution_case_sha256=execution_case_sha256,
                execution_case_set_sha256=execution_case_set_sha256,
                schedule_sha256=schedule_sha256,
                schedule_index=turn.schedule_index,
                execution_order=turn.execution_order,
                canonical_turn_id=turn.turn_id,
                observation_id=turn.observation_id,
                identity=turn.identity,
                namespace=namespace,
                limits=EvaluationResourceLimitsV3.from_budget(
                    package7_inputs.protocol.experiment.budget
                ),
                initial_state_reset=True,
                initial_state_reset_receipt=reset,
                turn_results=(turn_result,),
                totals=totals,
                pilot_cost=PilotTurnCostV3(
                    turn_id=turn.turn_id,
                    identity=turn.identity,
                    variant_id=turn.identity.variant_id,
                    case_id=turn.identity.case_id,
                    is_warmup=turn.identity.turn_kind is ScheduledTurnKindV3.WARMUP,
                    known_cost_usd=Decimal("0.01"),
                    ledger_attributed=True,
                    ledger_valid=True,
                    ledger_event_ids=(ledger_event.ledger_event_id,),
                ),
            )
        )
    return schedule, tuple(receipts)


def test_builds_exactly_32_deterministic_gold_derived_calibration_inputs(
    package7_inputs,
) -> None:
    schedule, receipts = _synthetic_receipts(package7_inputs)

    first = build_package8_pilot_calibration_inputs_v3(
        protocol=package7_inputs.protocol,
        loaded_gold=package7_inputs.loaded_gold,
        pilot_schedule=schedule,
        pilot_receipts=receipts,
        evidence_resolver=_Resolver(),
        policy=_policy(),
    )
    second = build_package8_pilot_calibration_inputs_v3(
        protocol=package7_inputs.protocol,
        loaded_gold=package7_inputs.loaded_gold,
        pilot_schedule=schedule,
        pilot_receipts=tuple(reversed(receipts)),
        evidence_resolver=_Resolver(),
        policy=_policy(),
    )

    assert len(first.cases) == 32
    assert len(first.references.references) == 32
    assert first.inputs_sha256 == second.inputs_sha256
    assert first.expected_calibration_ids == tuple(
        item.calibration_id for item in first.cases
    )
    assert {item.source_classification for item in first.references.references} == {
        "automated_gold_derived"
    }
    assert set(first.thresholds.maximum_absolute_error.values()) == {0.25}
    assert all(
        turn.identity.turn_kind is ScheduledTurnKindV3.MEASURED
        for turn in schedule
        if turn.observation_id
        in {
            reference.development_observation_id
            for reference in first.references.references
        }
    )


def test_bridge_rejects_missing_or_tampered_pilot_receipts(package7_inputs) -> None:
    schedule, receipts = _synthetic_receipts(package7_inputs)
    with pytest.raises(BenchmarkCalibrationValidationErrorV3, match="whole schedule"):
        build_package8_pilot_calibration_inputs_v3(
            protocol=package7_inputs.protocol,
            loaded_gold=package7_inputs.loaded_gold,
            pilot_schedule=schedule,
            pilot_receipts=receipts[:-1],
            evidence_resolver=_Resolver(),
            policy=_policy(),
        )

    tampered = receipts[0].model_copy(update={"observation_id": "observation_wrong"})
    with pytest.raises(
        BenchmarkCalibrationValidationErrorV3, match="differs from its scheduled"
    ):
        build_package8_pilot_calibration_inputs_v3(
            protocol=package7_inputs.protocol,
            loaded_gold=package7_inputs.loaded_gold,
            pilot_schedule=schedule,
            pilot_receipts=(tampered, *receipts[1:]),
            evidence_resolver=_Resolver(),
            policy=_policy(),
        )

    with pytest.raises(
        BenchmarkCalibrationValidationErrorV3, match="duplicate pilot receipt"
    ):
        build_package8_pilot_calibration_inputs_v3(
            protocol=package7_inputs.protocol,
            loaded_gold=package7_inputs.loaded_gold,
            pilot_schedule=schedule,
            pilot_receipts=(receipts[0], *receipts[:-1]),
            evidence_resolver=_Resolver(),
            policy=_policy(),
        )


def test_citations_require_the_exact_resolved_evidence() -> None:
    evidence = EvidenceReference(
        evidence_id="evidence_source",
        source_id="source_primary",
        source_version_id="source_version_one",
        chunk_id="chunk_primary",
        span_id="span_primary",
        display_label="[C1]",
        kind=EvidenceKind.KNOWLEDGE,
        title="Synthetic source",
        observed_at=datetime.now(UTC),
    )
    citation = Citation(
        citation_id="citation_source",
        claim_id="claim_source",
        evidence_id=evidence.evidence_id,
        span_id=evidence.span_id,
        display_label=evidence.display_label,
    )
    result = TurnResult(
        outcome=DialogueOutcome.ANSWERED,
        answer="Synthetic answer.",
        claims=(
            Claim(
                claim_id="claim_source",
                text="Synthetic",
                citation_ids=(citation.citation_id,),
            ),
        ),
        citations=(citation,),
        evidence=(evidence,),
    )
    binding = EvidenceBindingKeyV3(
        evidence_id=evidence.evidence_id,
        source_id=evidence.source_id,
        source_version_id=evidence.source_version_id,
        chunk_id=evidence.chunk_id,
        span_id=evidence.span_id,
    )

    assert (
        _exact_citations(result, _Resolver({binding: "immutable exact excerpt"}))[
            0
        ].evidence
        == "immutable exact excerpt"
    )
    with pytest.raises(
        BenchmarkCalibrationValidationErrorV3, match="could not be resolved"
    ):
        _exact_citations(result, _Resolver())


def test_policy_is_hash_stable_and_preserves_all_caller_thresholds() -> None:
    first = _policy()
    second = _policy()

    assert first == second
    assert len(first.maximum_absolute_error) == 6
    assert set(first.maximum_absolute_error.values()) == {0.25}
