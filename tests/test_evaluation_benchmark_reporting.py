"""Synthetic fail-closed tests for the Package 8 reporting boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts import TaskStatus
from app.evaluation.benchmark_reporting import (
    BenchmarkReportingValidationErrorV3,
    EvidenceBindingKeyV3,
    HeldOutReceiptAdmissionV3,
    JudgedObservationSetV3,
    Package8PartialExecutionReportV3,
    ReceiptAccountingV3,
    ReceiptFailureTaxonomyEntryV3,
    ResolvedExactEvidenceV3,
    account_observation_receipts_v3,
    analyze_judged_observations_v3,
    build_package8_partial_execution_report_v3,
    build_package8_results_summary_v3,
    convert_completed_receipts_to_provisionals_v3,
    join_model_judgments_to_observations_v3,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerPacketV3,
    BlindedAnswerV3,
    JudgmentRecordV3,
    RubricContextV3,
    UnblindingEntryV3,
    UnblindingKeyV3,
    build_judgment_record_v3,
)
from app.evaluation.v3_comparison import ArtifactBindingsV3
from app.evaluation.v3_gold import (
    AnswerabilityV3,
    EvaluationSplitV3,
    RequiredResponseModeV3,
    load_evaluation_gold_v3,
)
from app.evaluation.v3_judge import (
    COMPLETE_JUDGED_METRICS_V3,
    DETERMINISTIC_JUDGE_METRICS_V3,
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationFreezeV3,
    ModelJudgeConfigurationV3,
    build_model_judge_configuration_v3,
    evaluator_configuration_sha256_v3,
    judge_prompt_sha256_v3,
    model_judge_output_schema_sha256_v3,
)
from app.evaluation.v3_models import (
    PACKAGE7_VARIANT_ORDER,
    EvaluationAssetBindingsV3,
    EvaluationMetricV3,
    EvaluationObservationV3,
    GenerationBindingV3,
    ObservationIdentityV3,
    RepeatDecisionV3,
    ScheduledTurnKindV3,
    VariantCostProjectionV3,
    canonical_observation_id_v3,
    canonical_turn_id_v3,
)
from app.evaluation.v3_protocol import (
    build_evaluation_protocol_v3,
    evaluation_protocol_sha256_v3,
    load_evaluation_experiment_v3,
)
from app.evaluation.v3_runner import (
    EmbeddingCallEvidenceV3,
    EvaluationRunSummaryV3,
    ExecutionAttributionV3,
    InitialStateResetReceiptV3,
    LedgerEventEvidenceV3,
    LedgerEventKindV3,
    ModelCallEvidenceV3,
    ObservationResourceTotalsV3,
    ObservationRunReceiptV3,
    ObservationTerminalStatusV3,
    RetryEvidenceV3,
    UserTurnExecutionResultV3,
    execution_namespace_v3,
)
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
RUN_ID = "run_reporting_v3"
PROTOCOL_SHA256 = "a" * 64
SCHEDULE_SHA256 = "b" * 64
EXECUTION_CASE_SHA256 = "c" * 64
EXECUTION_CASE_SET_SHA256 = "d" * 64
MODEL = GenerationBindingV3(model="gpt-5.4-mini-2026-03-17")
PROMPT = "Return one calibrated verdict per required semantic metric."


class _Resolver:
    def __init__(self, entries: dict[EvidenceBindingKeyV3, str]) -> None:
        self.entries = entries

    def resolve(self, binding: EvidenceBindingKeyV3) -> ResolvedExactEvidenceV3 | None:
        text = self.entries.get(binding)
        return (
            None
            if text is None
            else ResolvedExactEvidenceV3(
                binding=binding,
                exact_text=text,
            )
        )


def test_partial_execution_report_preserves_failed_receipt_accounting() -> None:
    summary = EvaluationRunSummaryV3(
        run_id=RUN_ID,
        protocol_sha256=PROTOCOL_SHA256,
        execution_case_set_sha256=EXECUTION_CASE_SET_SHA256,
        schedule_sha256=SCHEDULE_SHA256,
        total_scheduled=3,
        completed_turn_ids=("turn_complete",),
        failed_turn_ids=("turn_failed_one", "turn_failed_two"),
        pending_turn_ids=(),
        missing_turn_ids=(),
        ambiguous_turn_ids=(),
        orphan_started_turn_ids=(),
        completed_observation_ids=("obs_complete",),
        is_partial=True,
        blocked_on_ambiguous_work=False,
    )
    accounting = ReceiptAccountingV3(
        receipt_count=3,
        completed_measured_receipt_count=1,
        failed_receipt_count=2,
        warmup_receipt_count=0,
        known_cost_usd=Decimal("0.012"),
        unresolved_reserved_cost_usd=Decimal("0"),
        input_tokens=8,
        output_tokens=4,
        total_tokens=12,
        generation_call_count=2,
        embedding_call_count=0,
        provider_attempt_count=2,
        retry_count=0,
        failures=(
            ReceiptFailureTaxonomyEntryV3(
                safe_error_code="provider_cost_limit_exceeded",
                receipt_count=2,
            ),
        ),
    )

    report = build_package8_partial_execution_report_v3(
        summary=summary,
        accounting=accounting,
        checkpoint_sha256="e" * 64,
    )

    assert isinstance(report, Package8PartialExecutionReportV3)
    assert report.completion_status == "partial"
    assert report.failed_turn_count == 2
    assert report.receipt_accounting == accounting

    ambiguous_accounting = accounting.model_copy(
        update={
            "receipt_count": 2,
            "failed_receipt_count": 1,
            "failures": (
                ReceiptFailureTaxonomyEntryV3(
                    safe_error_code="provider_cost_limit_exceeded",
                    receipt_count=1,
                ),
            ),
        }
    )
    ambiguous_report = build_package8_partial_execution_report_v3(
        summary=summary.model_copy(
            update={
                "failed_turn_ids": ("turn_failed_one",),
                "ambiguous_turn_ids": ("turn_ambiguous",),
            }
        ),
        accounting=ambiguous_accounting,
        checkpoint_sha256="f" * 64,
    )
    assert ambiguous_report.ambiguous_turn_count == 1

    with pytest.raises(BenchmarkReportingValidationErrorV3):
        build_package8_partial_execution_report_v3(
            summary=summary.model_copy(update={"is_partial": False}),
            accounting=accounting,
            checkpoint_sha256="e" * 64,
        )


def test_receipt_conversion_uses_only_actual_citation_evidence() -> None:
    receipt = _receipt(0)
    admission = _admission(receipt)
    resolver = _resolver_for_receipt(receipt)

    converted = convert_completed_receipts_to_provisionals_v3(
        (receipt,),
        admission=admission,
        evidence_resolver=resolver,
    )

    provisional = converted[0]
    assert (
        provisional.answer_evidence.answer == "Synthetic answer grounded in the source."
    )
    assert provisional.answer_evidence.citations[0].label == "[C1]"
    assert provisional.answer_evidence.citations[0].evidence == "Exact source span."
    assert provisional.operational.input_tokens == 13
    assert provisional.operational.output_tokens == 5
    assert provisional.operational.generation_call_count == 1
    assert provisional.operational.embedding_call_count == 1
    assert provisional.operational.retry_count == 1
    assert provisional.observation.status is TaskStatus.PARTIAL_SUCCESS

    with pytest.raises(
        BenchmarkReportingValidationErrorV3, match="did not return exact"
    ):
        convert_completed_receipts_to_provisionals_v3(
            (receipt,),
            admission=admission,
            evidence_resolver=_Resolver({}),
        )

    payload = receipt.turn_results[-1].result_payload
    assert isinstance(payload["result"], dict)
    fabricated_payload = {
        **payload,
        "result": {
            **payload["result"],
            "citations": [
                {
                    **payload["result"]["citations"][0],
                    "evidence_id": "evidence_fabricated",
                }
            ],
        },
    }
    fabricated_result = receipt.turn_results[-1].model_copy(
        update={"result_payload": fabricated_payload}
    )
    fabricated_receipt = receipt.model_copy(
        update={"turn_results": (fabricated_result,)}
    )
    with pytest.raises(
        BenchmarkReportingValidationErrorV3, match="final result payload"
    ):
        convert_completed_receipts_to_provisionals_v3(
            (fabricated_receipt,),
            admission=admission,
            evidence_resolver=resolver,
        )


def test_source_level_catalog_citation_resolves_server_owned_exact_text() -> None:
    receipt = _receipt(
        2,
        evidence_kind=EvidenceKind.CATALOG,
        include_location=False,
    )

    converted = convert_completed_receipts_to_provisionals_v3(
        (receipt,),
        admission=_admission(receipt),
        evidence_resolver=_resolver_for_receipt(receipt),
    )

    binding = converted[0].authoritative_evidence[0]
    assert binding.chunk_id is None
    assert binding.span_id is None
    assert converted[0].answer_evidence.citations[0].evidence == "Exact source span."


def test_foreign_failed_and_warmup_receipts_are_rejected() -> None:
    receipt = _receipt(0)
    resolver = _resolver_for_receipt(receipt)
    foreign_admission = HeldOutReceiptAdmissionV3(
        run_id=RUN_ID,
        protocol_sha256="f" * 64,
        schedule_sha256=SCHEDULE_SHA256,
        expected_observation_ids=(receipt.observation_id,),
    )
    with pytest.raises(BenchmarkReportingValidationErrorV3, match="foreign"):
        convert_completed_receipts_to_provisionals_v3(
            (receipt,),
            admission=foreign_admission,
            evidence_resolver=resolver,
        )

    failed = receipt.model_copy(
        update={
            "terminal_status": ObservationTerminalStatusV3.FAILED,
            "safe_error_code": "synthetic_failure",
        }
    )
    with pytest.raises(BenchmarkReportingValidationErrorV3, match="only completed"):
        convert_completed_receipts_to_provisionals_v3(
            (failed,),
            admission=_admission(receipt),
            evidence_resolver=resolver,
        )

    warmup = _receipt(1, turn_kind=ScheduledTurnKindV3.WARMUP)
    with pytest.raises(BenchmarkReportingValidationErrorV3, match="warmup"):
        convert_completed_receipts_to_provisionals_v3(
            (warmup,),
            admission=HeldOutReceiptAdmissionV3(
                run_id=RUN_ID,
                protocol_sha256=PROTOCOL_SHA256,
                schedule_sha256=SCHEDULE_SHA256,
                expected_observation_ids=("obs_not_used",),
            ),
            evidence_resolver=_resolver_for_receipt(warmup),
        )


def test_batch_rejects_reused_ledger_event_ids() -> None:
    first = _receipt(0, ledger_event_id="ledger_shared")
    second = _receipt(1, ledger_event_id="ledger_shared")

    with pytest.raises(BenchmarkReportingValidationErrorV3, match="reuses a ledger"):
        convert_completed_receipts_to_provisionals_v3(
            (first, second),
            admission=HeldOutReceiptAdmissionV3(
                run_id=RUN_ID,
                protocol_sha256=PROTOCOL_SHA256,
                schedule_sha256=SCHEDULE_SHA256,
                expected_observation_ids=(first.observation_id, second.observation_id),
            ),
            evidence_resolver=_Resolver(
                {
                    **_resolver_for_receipt(first).entries,
                    **_resolver_for_receipt(second).entries,
                }
            ),
        )


def test_complete_blind_key_and_model_judgment_join_is_exact() -> None:
    provisional = _provisional()
    packet, key, configuration, calibration = _blind_context(provisional)
    judgment = _judgment(packet, configuration, calibration)

    joined = join_model_judgments_to_observations_v3(
        (provisional,),
        blinded_packet=packet,
        unblinding_key=key,
        judgments=(judgment,),
        judge_configuration=configuration,
        calibration=calibration,
    )

    observation = joined.observations[0]
    assert observation.status is TaskStatus.SUCCESS
    assert observation.task_completed is True
    assert observation.claim_support == 1.0
    assert observation.document_recall == 1.0
    assert observation.ledger_event_ids == provisional.observation.ledger_event_ids
    assert joined.authoritative_observation_ids == (observation.observation_id,)

    no_citation = _provisional(include_citation=False)
    empty_packet, empty_key, empty_config, empty_calibration = _blind_context(
        no_citation
    )
    citationless_joined = join_model_judgments_to_observations_v3(
        (no_citation,),
        blinded_packet=empty_packet,
        unblinding_key=empty_key,
        judgments=(_judgment(empty_packet, empty_config, empty_calibration),),
        judge_configuration=empty_config,
        calibration=empty_calibration,
    )
    assert citationless_joined.authoritative_observation_ids == ()
    assert citationless_joined.observations[0].document_recall == 1.0


def test_judgment_join_rejects_mismatched_calibration_model_and_scores() -> None:
    provisional = _provisional()
    packet, key, configuration, calibration = _blind_context(provisional)
    wrong_calibration = _calibration(
        protocol_sha256=calibration.protocol_sha256,
        configuration_sha256=configuration.configuration_sha256,
        marker="e",
    )
    with pytest.raises(BenchmarkReportingValidationErrorV3, match="calibration"):
        join_model_judgments_to_observations_v3(
            (provisional,),
            blinded_packet=packet,
            unblinding_key=key,
            judgments=(_judgment(packet, configuration, wrong_calibration),),
            judge_configuration=configuration,
            calibration=calibration,
        )

    incomplete = build_judgment_record_v3(
        bindings=packet.bindings,
        blinded_packet_sha256=packet.packet_sha256,
        opaque_answer_id=packet.answers[0].opaque_answer_id,
        judgment_mode="model_judge",
        model_binding=MODEL,
        scores={EvaluationMetricV3.CLAIM_SUPPORT: 1.0},
        score_sources={EvaluationMetricV3.CLAIM_SUPPORT: "model_judge"},
        judge_configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=calibration.calibration_sha256,
        judge_output_sha256="e" * 64,
    )
    with pytest.raises(BenchmarkReportingValidationErrorV3, match="complete required"):
        join_model_judgments_to_observations_v3(
            (provisional,),
            blinded_packet=packet,
            unblinding_key=key,
            judgments=(incomplete,),
            judge_configuration=configuration,
            calibration=calibration,
        )

    malformed_model = _judgment(packet, configuration, calibration).model_copy(
        update={"model_binding": None}
    )
    with pytest.raises(BenchmarkReportingValidationErrorV3, match="judgment record"):
        join_model_judgments_to_observations_v3(
            (provisional,),
            blinded_packet=packet,
            unblinding_key=key,
            judgments=(malformed_model,),
            judge_configuration=configuration,
            calibration=calibration,
        )


def test_validated_analysis_preserves_partial_completion_and_summary_accounting() -> (
    None
):
    protocol, loaded_gold, repeat_decision, observation = _partial_analysis_context()
    bindings = _bindings(protocol_sha256=evaluation_protocol_sha256_v3(protocol))
    judged = JudgedObservationSetV3(
        bindings=bindings,
        judge_configuration_sha256=bindings.evaluator_configuration_sha256,
        calibration_sha256="e" * 64,
        observations=(observation,),
        authoritative_observation_ids=(observation.observation_id,),
    )

    result = analyze_judged_observations_v3(
        judged,
        protocol=protocol,
        split=loaded_gold.split,
        repeat_decision=repeat_decision,
        run_id=observation.run_id,
        gold_sha256=loaded_gold.gold_sha256,
        split_sha256=loaded_gold.split_sha256,
        schedule_sha256="f" * 64,
        bootstrap_samples=100,
    )

    assert result.analysis.completion_status == "partial"
    assert result.analysis.accepted_observation_count == 1
    receipt = _receipt(0)
    accounting = account_observation_receipts_v3((receipt,))
    summary = build_package8_results_summary_v3(
        accounting=accounting,
        judged=judged.model_copy(update={"bindings": result.analysis.bindings}),
        analysis=result.analysis,
        report_sha256=result.report_sha256,
    )
    assert summary.completion_status == "partial"
    assert summary.total_tokens == summary.input_tokens + summary.output_tokens
    assert summary.document_recall_authority_count == 1


def _admission(receipt: ObservationRunReceiptV3) -> HeldOutReceiptAdmissionV3:
    assert receipt.observation_id is not None
    return HeldOutReceiptAdmissionV3(
        run_id=RUN_ID,
        protocol_sha256=PROTOCOL_SHA256,
        schedule_sha256=SCHEDULE_SHA256,
        expected_observation_ids=(receipt.observation_id,),
    )


def _receipt(
    index: int,
    *,
    turn_kind: ScheduledTurnKindV3 = ScheduledTurnKindV3.MEASURED,
    ledger_event_id: str | None = None,
    evidence_kind: EvidenceKind = EvidenceKind.KNOWLEDGE,
    include_location: bool = True,
) -> ObservationRunReceiptV3:
    identity = ObservationIdentityV3(
        run_id=RUN_ID,
        protocol_sha256=PROTOCOL_SHA256,
        schedule_algorithm_id="package7_pilot_interleaved_v1",
        turn_kind=turn_kind,
        variant_id="sa_shared_tools_rag",
        case_id=f"case_{index + 1:03d}",
        work_group_id=f"group_{index + 1:03d}",
        repetition=0,
    )
    canonical_turn_id = canonical_turn_id_v3(identity)
    observation_id = (
        canonical_observation_id_v3(identity)
        if turn_kind is ScheduledTurnKindV3.MEASURED
        else None
    )
    namespace = execution_namespace_v3(
        run_id=RUN_ID,
        protocol_sha256=PROTOCOL_SHA256,
        execution_case_sha256=EXECUTION_CASE_SHA256,
        execution_case_set_sha256=EXECUTION_CASE_SET_SHA256,
        schedule_sha256=SCHEDULE_SHA256,
        canonical_turn_id=canonical_turn_id,
        observation_id=observation_id,
    )
    attribution = ExecutionAttributionV3(
        run_id=RUN_ID,
        canonical_turn_id=canonical_turn_id,
        observation_id=observation_id,
        execution_turn_id=f"eturn_{index + 1:064x}",
        source_turn_id=f"source_{index + 1:03d}",
    )
    final_result = _turn_result(
        include_citation=True,
        evidence_kind=evidence_kind,
        include_location=include_location,
    )
    durable = DurableTurnOutcome(
        turn_id=f"durable_{index + 1:03d}",
        status=TurnStatus.COMPLETED,
        outcome=final_result.outcome,
        result=final_result,
    )
    model_call = ModelCallEvidenceV3(
        call_id=f"mcall_{index + 1:03d}",
        attribution=attribution,
        model="gpt-5.4-mini-2026-03-17",
        attempts=2,
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=5,
        reasoning_tokens=1,
        total_tokens=15,
    )
    embedding_call = EmbeddingCallEvidenceV3(
        call_id=f"ecall_{index + 1:03d}",
        attribution=attribution,
        model="text-embedding-3-small",
        attempts=1,
        input_tokens=3,
    )
    ledger_id = ledger_event_id or f"ledger_{index + 1:03d}"
    result = UserTurnExecutionResultV3(
        attribution=attribution,
        result_payload=durable.model_dump(mode="json"),
        model_calls=(model_call,),
        embedding_calls=(embedding_call,),
        retry_events=(
            RetryEvidenceV3(
                retry_event_id=f"retry_{index + 1:03d}",
                attribution=attribution,
                call_id=model_call.call_id,
                retry_ordinal=1,
            ),
        ),
        ledger_events=(
            LedgerEventEvidenceV3(
                ledger_event_id=ledger_id,
                attribution=attribution,
                kind=LedgerEventKindV3.SETTLED_KNOWN,
                known_cost_usd=Decimal("0.01"),
            ),
        ),
        peak_provider_concurrency=1,
        max_attempt_duration_seconds=0.1,
        elapsed_seconds=0.2,
    )
    totals = ObservationResourceTotalsV3(
        generation_calls=1,
        provider_attempts=3,
        retry_count=1,
        peak_provider_concurrency=1,
        input_tokens=13,
        output_tokens=5,
        known_cost_usd=Decimal("0.01"),
        unresolved_reserved_cost_usd=Decimal("0"),
    )
    reset = InitialStateResetReceiptV3(
        reset_id=f"reset_{index + 1:064x}",
        run_id=RUN_ID,
        execution_case_sha256=EXECUTION_CASE_SHA256,
        execution_case_set_sha256=EXECUTION_CASE_SET_SHA256,
        canonical_turn_id=canonical_turn_id,
        observation_id=observation_id,
        namespace_id=namespace.namespace_id,
        case_id=identity.case_id,
        work_group_id=identity.work_group_id,
        initial_state_sha256="e" * 64,
    )
    from app.evaluation.v3_models import PilotTurnCostV3

    return ObservationRunReceiptV3(
        terminal_status=ObservationTerminalStatusV3.COMPLETED,
        run_id=RUN_ID,
        protocol_sha256=PROTOCOL_SHA256,
        execution_case_sha256=EXECUTION_CASE_SHA256,
        execution_case_set_sha256=EXECUTION_CASE_SET_SHA256,
        schedule_sha256=SCHEDULE_SHA256,
        schedule_index=index,
        execution_order=index if turn_kind is ScheduledTurnKindV3.MEASURED else None,
        canonical_turn_id=canonical_turn_id,
        observation_id=observation_id,
        identity=identity,
        namespace=namespace,
        limits={
            "provider_concurrency": 2,
            "max_generation_calls": 10,
            "max_provider_attempts": 16,
            "max_retries": 1,
            "attempt_timeout_seconds": 18.0,
            "turn_deadline_seconds": 60.0,
            "per_turn_limit_usd": "0.25",
            "max_input_tokens_per_generation": 12_000,
            "max_output_tokens_per_generation": 1_200,
        },
        initial_state_reset=True,
        initial_state_reset_receipt=reset,
        turn_results=(result,),
        totals=totals,
        pilot_cost=PilotTurnCostV3(
            turn_id=canonical_turn_id,
            identity=identity,
            variant_id=identity.variant_id,
            case_id=identity.case_id,
            is_warmup=turn_kind is ScheduledTurnKindV3.WARMUP,
            known_cost_usd=Decimal("0.01"),
            ledger_attributed=True,
            ledger_valid=True,
            ledger_event_ids=(ledger_id,),
        ),
    )


def _turn_result(
    *,
    include_citation: bool,
    evidence_kind: EvidenceKind = EvidenceKind.KNOWLEDGE,
    include_location: bool = True,
) -> TurnResult:
    evidence = EvidenceReference(
        evidence_id="evidence_source",
        source_id="source_primary",
        source_version_id="source_version_one",
        chunk_id="chunk_primary" if include_location else None,
        span_id="span_primary" if include_location else None,
        display_label="[C1]",
        kind=evidence_kind,
        title="Synthetic source",
        observed_at=datetime.now(UTC),
    )
    citations = ()
    claims = ()
    if include_citation:
        citation = Citation(
            citation_id="citation_source",
            claim_id="claim_source",
            evidence_id=evidence.evidence_id,
            span_id=evidence.span_id,
            display_label=evidence.display_label,
        )
        citations = (citation,)
        claims = (
            Claim(
                claim_id=citation.claim_id,
                text="Synthetic sourced claim.",
                citation_ids=(citation.citation_id,),
            ),
        )
    return TurnResult(
        outcome=DialogueOutcome.ANSWERED,
        answer="Synthetic answer grounded in the source.",
        claims=claims,
        citations=citations,
        evidence=(evidence,),
    )


def _resolver_for_receipt(receipt: ObservationRunReceiptV3) -> _Resolver:
    result = receipt.turn_results[-1].result_payload
    assert isinstance(result["result"], dict)
    evidence = result["result"]["evidence"][0]
    binding = EvidenceBindingKeyV3(
        evidence_id=evidence["evidence_id"],
        source_id=evidence["source_id"],
        source_version_id=evidence["source_version_id"],
        chunk_id=evidence["chunk_id"],
        span_id=evidence["span_id"],
    )
    return _Resolver({binding: "Exact source span."})


def _provisional(*, include_citation: bool = True):
    receipt = _receipt(0)
    if not include_citation:
        final = _turn_result(include_citation=False)
        payload = receipt.turn_results[-1].result_payload
        without_citation = {
            **payload,
            "outcome": final.outcome.value,
            "result": final.model_dump(mode="json"),
        }
        receipt = receipt.model_copy(
            update={
                "turn_results": (
                    receipt.turn_results[-1].model_copy(
                        update={"result_payload": without_citation}
                    ),
                )
            }
        )
    return convert_completed_receipts_to_provisionals_v3(
        (receipt,),
        admission=_admission(receipt),
        evidence_resolver=_resolver_for_receipt(_receipt(0)),
    )[0]


def _blind_context(provisional):
    bindings = _bindings(protocol_sha256=provisional.observation.protocol_sha256)
    configuration = build_model_judge_configuration_v3(
        bindings=bindings,
        model_binding=MODEL,
        judge_prompt=PROMPT,
    )
    answer = BlindedAnswerV3(
        opaque_answer_id="answer_000000000000000000000001",
        prompt=("Synthetic blind prompt",),
        answer=provisional.answer_evidence.answer,
        citations=provisional.answer_evidence.citations,
        rubric_context=RubricContextV3(
            answerability=AnswerabilityV3.ANSWERABLE,
            required_response_mode=RequiredResponseModeV3.DIRECT_ANSWER,
            required_facts=(),
            forbidden_claims=(),
            expected_action_outcome="complete",
        ),
    )
    packet_payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.HELD_OUT,
        "bindings": bindings.model_dump(mode="json"),
        "randomization_seed": 42,
        "answer_count": 1,
        "answers": [answer.model_dump(mode="json")],
    }
    packet = BlindedAnswerPacketV3(
        bindings=bindings,
        randomization_seed=42,
        answer_count=1,
        answers=(answer,),
        packet_sha256=canonical_sha256(packet_payload),
    )
    entry = UnblindingEntryV3(
        opaque_answer_id=answer.opaque_answer_id,
        observation_id=provisional.observation.observation_id,
        variant_id=provisional.observation.variant_id,
        conversation_id=provisional.observation.case_id,
        work_group_id=provisional.observation.work_group_id,
        repetition=provisional.observation.repetition,
    )
    key_payload = {
        "schema_version": "3.0",
        "bindings": bindings.model_dump(mode="json"),
        "blinded_packet_sha256": packet.packet_sha256,
        "entries": [entry.model_dump(mode="json")],
    }
    key = UnblindingKeyV3(
        bindings=bindings,
        blinded_packet_sha256=packet.packet_sha256,
        entries=(entry,),
        key_sha256=canonical_sha256(key_payload),
    )
    calibration = _calibration(
        protocol_sha256=bindings.protocol_sha256,
        configuration_sha256=configuration.configuration_sha256,
        marker="d",
    )
    return packet, key, configuration, calibration


def _bindings(*, protocol_sha256: str) -> ArtifactBindingsV3:
    prompt_sha = judge_prompt_sha256_v3(PROMPT)
    schema_sha = model_judge_output_schema_sha256_v3()
    rubric_sha = "d" * 64
    return ArtifactBindingsV3(
        protocol_sha256=protocol_sha256,
        gold_sha256="1" * 64,
        split_sha256="2" * 64,
        schedule_sha256="3" * 64,
        repeat_decision_sha256="4" * 64,
        evaluator_configuration_sha256=evaluator_configuration_sha256_v3(
            model_binding=MODEL,
            judge_prompt_sha256=prompt_sha,
            judge_schema_sha256=schema_sha,
            rubric_sha256=rubric_sha,
        ),
        judge_prompt_sha256=prompt_sha,
        judge_schema_sha256=schema_sha,
        tool_contract_sha256="5" * 64,
        corpus_sha256="6" * 64,
        index_sha256="7" * 64,
        embedding_model_dimensions_sha256="8" * 64,
        pricing_manifest_sha256="9" * 64,
        usage_ledger_sha256="a" * 64,
        rubric_sha256=rubric_sha,
    )


def _calibration(*, protocol_sha256: str, configuration_sha256: str, marker: str):
    payload = {
        "schema_version": "3.0",
        "protocol_sha256": protocol_sha256,
        "configuration_sha256": configuration_sha256,
        "thresholds_sha256": "1" * 64,
        "reference_bundle_sha256": "2" * 64,
        "expected_calibration_ids": ("calibration_one",),
        "development_record_sha256s": (marker * 64,),
        "maximum_observed_errors": {
            metric: 0.0 for metric in SEMANTIC_JUDGE_METRICS_V3
        },
    }
    return CalibrationFreezeV3(
        **payload,
        calibration_sha256=canonical_sha256(payload),
    )


def _judgment(
    packet: BlindedAnswerPacketV3,
    configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
) -> JudgmentRecordV3:
    scores = {metric: 1.0 for metric in COMPLETE_JUDGED_METRICS_V3}
    sources = {
        **{metric: "model_judge" for metric in SEMANTIC_JUDGE_METRICS_V3},
        **{metric: "deterministic" for metric in DETERMINISTIC_JUDGE_METRICS_V3},
    }
    return build_judgment_record_v3(
        bindings=packet.bindings,
        blinded_packet_sha256=packet.packet_sha256,
        opaque_answer_id=packet.answers[0].opaque_answer_id,
        judgment_mode="model_judge",
        model_binding=MODEL,
        scores=scores,
        score_sources=sources,
        judge_configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=calibration.calibration_sha256,
        judge_output_sha256="e" * 64,
    )


def _partial_analysis_context():
    loaded_gold = load_evaluation_gold_v3(
        PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
        PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
        project_root=PROJECT_ROOT,
    )
    asset_payload = {
        field_name: f"{index + 1:064x}"
        for index, field_name in enumerate(EvaluationAssetBindingsV3.model_fields)
    }
    asset_payload.update(
        {
            "gold_sha256": loaded_gold.gold_sha256,
            "split_sha256": loaded_gold.split_sha256,
        }
    )
    assets = EvaluationAssetBindingsV3.model_validate(asset_payload)
    protocol = build_evaluation_protocol_v3(
        load_evaluation_experiment_v3(
            PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"
        ),
        assets=assets,
    )
    protocol_sha = evaluation_protocol_sha256_v3(protocol)
    repeat_decision = RepeatDecisionV3(
        protocol_sha256=protocol_sha,
        repeat_rule_sha256=protocol.repeat_rule_sha256,
        cost_evidence_sha256="9" * 64,
        selected_repeats=2,
        pilot_effective_cost_usd=Decimal("1"),
        projected_benchmark_cost_usd=Decimal("20"),
        per_variant=tuple(
            VariantCostProjectionV3(
                variant_id=variant_id,
                maximum_pilot_observation_cost_usd=Decimal("0.01"),
            )
            for variant_id in PACKAGE7_VARIANT_ORDER
        ),
    )
    case = next(
        item
        for item in loaded_gold.gold.conversations
        if item.split is EvaluationSplitV3.HELD_OUT
    )
    identity = ObservationIdentityV3(
        run_id="run_partial_reporting",
        protocol_sha256=protocol_sha,
        schedule_algorithm_id=protocol.schedule_algorithm_id,
        turn_kind=ScheduledTurnKindV3.MEASURED,
        variant_id="sa_shared_tools_rag",
        case_id=case.conversation_id,
        work_group_id=case.work_group_id,
        repetition=0,
    )
    observation = EvaluationObservationV3(
        observation_id=canonical_observation_id_v3(identity),
        identity=identity,
        run_id=identity.run_id,
        protocol_sha256=identity.protocol_sha256,
        variant_id=identity.variant_id,
        case_id=identity.case_id,
        work_group_id=identity.work_group_id,
        repetition=identity.repetition,
        execution_order=0,
        status=TaskStatus.SUCCESS,
        task_completed=True,
        answerable=True,
        abstained=False,
        claim_support=1.0,
        citation_precision=1.0,
        citation_coverage=1.0,
        document_recall=1.0,
        authorized=True,
        valid_plan=True,
        useful_continuation=True,
        duplicate_dispatch_count=0,
        end_to_end_latency_ms=200.0,
        input_tokens=13,
        output_tokens=5,
        total_tokens=18,
        known_cost_usd=Decimal("0.01"),
        unresolved_reserved_cost_usd=Decimal("0"),
        ledger_event_ids=("ledger_partial",),
    )
    return protocol, loaded_gold, repeat_decision, observation
