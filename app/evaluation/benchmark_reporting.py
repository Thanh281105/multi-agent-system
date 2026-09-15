"""Fail-closed Package 8 boundary from execution receipts to v3 reporting.

Package 7 owns execution and its immutable identities.  This module deliberately
does not execute models, retrieve documents, score a rubric, or write artifacts.
It admits genuine terminal receipts, reopens only the evidence referenced by the
final durable ``TurnResult``, and makes the hand-off to the existing blinded
judgment and analysis contracts explicit.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.contracts import TaskStatus
from app.evaluation.v3_artifacts import (
    AnswerEvidenceInputV3,
    BlindedAnswerArtifactsV3,
    BlindedAnswerPacketV3,
    CitationForReviewV3,
    JudgmentRecordV3,
    UnblindingKeyV3,
    build_evaluation_report_v3,
)
from app.evaluation.v3_comparison import (
    ArtifactBindingsV3,
    EvaluationAnalysisV3,
    analyze_evaluation_v3,
)
from app.evaluation.v3_gold import EvaluationSplitManifestV3
from app.evaluation.v3_judge import (
    COMPLETE_JUDGED_METRICS_V3,
    DETERMINISTIC_JUDGE_METRICS_V3,
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationFreezeV3,
    ModelJudgeConfigurationV3,
)
from app.evaluation.v3_models import (
    EvaluationMetricV3,
    EvaluationObservationV3,
    EvaluationProtocolV3,
    RepeatDecisionV3,
    ScheduledTurnKindV3,
)
from app.evaluation.v3_runner import (
    EvaluationRunSummaryV3,
    ObservationRunReceiptV3,
    ObservationTerminalStatusV3,
)
from app.v2.contracts import DialogueOutcome, TurnResult, TurnStatus
from app.v2.execution import DurableTurnOutcome

_IDENTIFIER = r"^[a-z][a-z0-9_-]{2,127}$"
_V3_IDENTIFIER = r"^[a-z][a-z0-9_.-]{2,127}$"
_SHA256 = r"^[a-f0-9]{64}$"


class BenchmarkReportingValidationErrorV3(ValueError):
    """Raised when untrusted execution or judgment data cannot be reported."""


class FrozenBenchmarkReportingContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class HeldOutReceiptAdmissionV3(FrozenBenchmarkReportingContractV3):
    """The closed identity set that a receipt batch is allowed to populate."""

    run_id: str = Field(pattern=_V3_IDENTIFIER)
    protocol_sha256: str = Field(pattern=_SHA256)
    schedule_sha256: str = Field(pattern=_SHA256)
    expected_observation_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expected_ids(self) -> HeldOutReceiptAdmissionV3:
        if len(self.expected_observation_ids) != len(
            set(self.expected_observation_ids)
        ):
            raise ValueError(
                "held-out receipt admission contains duplicate observations"
            )
        return self


class EvidenceBindingKeyV3(FrozenBenchmarkReportingContractV3):
    """Immutable location of one exact evidence span used by a final answer."""

    evidence_id: str = Field(pattern=_IDENTIFIER)
    source_id: str = Field(pattern=_IDENTIFIER)
    source_version_id: str = Field(pattern=_IDENTIFIER)
    chunk_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    span_id: str | None = Field(default=None, pattern=_IDENTIFIER)


class ResolvedExactEvidenceV3(FrozenBenchmarkReportingContractV3):
    """Resolver output; text is never derived from a rubric or display label."""

    binding: EvidenceBindingKeyV3
    exact_text: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def validate_exact_text(self) -> ResolvedExactEvidenceV3:
        if not self.exact_text.strip():
            raise ValueError("resolved exact evidence cannot be blank")
        return self


class ImmutableEvidenceResolverV3(Protocol):
    """Injected read-only resolver for immutable source/version/chunk/span keys."""

    def resolve(
        self, binding: EvidenceBindingKeyV3
    ) -> ResolvedExactEvidenceV3 | None: ...


class ReceiptOperationalEvidenceV3(FrozenBenchmarkReportingContractV3):
    """Execution evidence copied exactly from a validated observation receipt."""

    canonical_turn_id: str = Field(pattern=_V3_IDENTIFIER)
    ledger_event_ids: tuple[str, ...] = Field(min_length=1)
    known_cost_usd: Decimal = Field(ge=0)
    unresolved_reserved_cost_usd: Decimal = Field(ge=0)
    generation_call_count: int = Field(ge=0)
    embedding_call_count: int = Field(ge=0)
    provider_attempt_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    end_to_end_latency_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_operational_evidence(self) -> ReceiptOperationalEvidenceV3:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("receipt evidence token totals do not reconcile")
        if len(self.ledger_event_ids) != len(set(self.ledger_event_ids)):
            raise ValueError("receipt evidence reuses ledger IDs")
        return self

    @property
    def effective_cost_usd(self) -> Decimal:
        return self.known_cost_usd + self.unresolved_reserved_cost_usd


class ProvisionalObservationV3(FrozenBenchmarkReportingContractV3):
    """Receipt-derived data that is intentionally not yet semantic scoring."""

    observation: EvaluationObservationV3
    answer_evidence: AnswerEvidenceInputV3
    final_turn_result: TurnResult
    authoritative_evidence: tuple[EvidenceBindingKeyV3, ...] = ()
    operational: ReceiptOperationalEvidenceV3

    @model_validator(mode="after")
    def validate_provisional_binding(self) -> ProvisionalObservationV3:
        observation = self.observation
        answer = self.answer_evidence
        if (
            answer.observation_id,
            answer.variant_id,
            answer.conversation_id,
            answer.work_group_id,
            answer.repetition,
        ) != (
            observation.observation_id,
            observation.variant_id,
            observation.case_id,
            observation.work_group_id,
            observation.repetition,
        ):
            raise ValueError("answer evidence does not match its receipt observation")
        if (
            self.operational.ledger_event_ids,
            self.operational.known_cost_usd,
            self.operational.unresolved_reserved_cost_usd,
            self.operational.input_tokens,
            self.operational.output_tokens,
            self.operational.total_tokens,
            self.operational.end_to_end_latency_ms,
        ) != (
            observation.ledger_event_ids,
            observation.known_cost_usd,
            observation.unresolved_reserved_cost_usd,
            observation.input_tokens,
            observation.output_tokens,
            observation.total_tokens,
            observation.end_to_end_latency_ms,
        ):
            raise ValueError("operational evidence does not match its observation")
        if len(self.authoritative_evidence) != len(set(self.authoritative_evidence)):
            raise ValueError("authoritative evidence bindings must be unique")
        return self


class ReceiptFailureTaxonomyEntryV3(FrozenBenchmarkReportingContractV3):
    safe_error_code: str = Field(pattern=_V3_IDENTIFIER)
    receipt_count: int = Field(ge=1)


class ReceiptAccountingV3(FrozenBenchmarkReportingContractV3):
    """Serializable actual receipt accounting, including terminal failure codes."""

    receipt_count: int = Field(ge=0)
    completed_measured_receipt_count: int = Field(ge=0)
    failed_receipt_count: int = Field(ge=0)
    warmup_receipt_count: int = Field(ge=0)
    known_cost_usd: Decimal = Field(ge=0)
    unresolved_reserved_cost_usd: Decimal = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    generation_call_count: int = Field(ge=0)
    embedding_call_count: int = Field(ge=0)
    provider_attempt_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    failures: tuple[ReceiptFailureTaxonomyEntryV3, ...] = ()

    @model_validator(mode="after")
    def validate_accounting(self) -> ReceiptAccountingV3:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("receipt accounting token totals do not reconcile")
        codes = tuple(item.safe_error_code for item in self.failures)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("failure taxonomy must be sorted and unique")
        if (
            sum(item.receipt_count for item in self.failures)
            != self.failed_receipt_count
        ):
            raise ValueError("failure taxonomy does not cover every failed receipt")
        return self

    @property
    def effective_cost_usd(self) -> Decimal:
        return self.known_cost_usd + self.unresolved_reserved_cost_usd


class Package8PartialExecutionReportV3(FrozenBenchmarkReportingContractV3):
    """Durable, non-scoring record for a terminal but incomplete P8 SUT run."""

    schema_version: Literal["8.0"] = "8.0"
    completion_status: Literal["partial"] = "partial"
    run_id: str = Field(pattern=_V3_IDENTIFIER)
    package7_protocol_sha256: str = Field(pattern=_SHA256)
    execution_case_set_sha256: str = Field(pattern=_SHA256)
    schedule_sha256: str = Field(pattern=_SHA256)
    checkpoint_sha256: str = Field(pattern=_SHA256)
    scheduled_turn_count: int = Field(ge=1)
    completed_turn_count: int = Field(ge=0)
    failed_turn_count: int = Field(ge=0)
    pending_turn_count: int = Field(ge=0)
    missing_turn_count: int = Field(ge=0)
    ambiguous_turn_count: int = Field(ge=0)
    orphan_started_turn_count: int = Field(ge=0)
    blocked_on_ambiguous_work: bool
    receipt_accounting: ReceiptAccountingV3

    @model_validator(mode="after")
    def validate_terminal_accounting(self) -> Package8PartialExecutionReportV3:
        terminal_count = self.completed_turn_count + self.failed_turn_count
        if self.receipt_accounting.receipt_count != terminal_count:
            raise ValueError(
                "partial report receipt accounting does not match terminals"
            )
        if (
            self.receipt_accounting.completed_measured_receipt_count
            != self.completed_turn_count
            or self.receipt_accounting.failed_receipt_count != self.failed_turn_count
        ):
            raise ValueError(
                "partial report terminal kinds do not match receipt accounting"
            )
        if (
            terminal_count + self.pending_turn_count + self.ambiguous_turn_count
            != self.scheduled_turn_count
        ):
            raise ValueError(
                "partial report scheduled count does not match terminal state"
            )
        return self


def build_package8_partial_execution_report_v3(
    *,
    summary: EvaluationRunSummaryV3,
    accounting: ReceiptAccountingV3,
    checkpoint_sha256: str,
) -> Package8PartialExecutionReportV3:
    """Record truthful SUT coverage without admitting failed receipts to scoring."""

    if not summary.is_partial:
        raise BenchmarkReportingValidationErrorV3(
            "complete execution cannot produce a partial report"
        )
    return Package8PartialExecutionReportV3(
        run_id=summary.run_id,
        package7_protocol_sha256=summary.protocol_sha256,
        execution_case_set_sha256=summary.execution_case_set_sha256,
        schedule_sha256=summary.schedule_sha256,
        checkpoint_sha256=checkpoint_sha256,
        scheduled_turn_count=summary.total_scheduled,
        completed_turn_count=len(summary.completed_turn_ids),
        failed_turn_count=len(summary.failed_turn_ids),
        pending_turn_count=len(summary.pending_turn_ids),
        missing_turn_count=len(summary.missing_turn_ids),
        ambiguous_turn_count=len(summary.ambiguous_turn_ids),
        orphan_started_turn_count=len(summary.orphan_started_turn_ids),
        blocked_on_ambiguous_work=summary.blocked_on_ambiguous_work,
        receipt_accounting=accounting,
    )


class JudgeAccountingV3(FrozenBenchmarkReportingContractV3):
    """Retry-inclusive immutable accounting from P8 judge journal terminals."""

    development_job_count: int = Field(ge=0)
    heldout_job_count: int = Field(ge=0)
    completed_job_count: int = Field(ge=0)
    failed_job_count: int = Field(ge=0)
    ambiguous_job_count: int = Field(ge=0)
    known_cost_usd: Decimal = Field(ge=0)
    unresolved_reserved_cost_usd: Decimal = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    generation_call_count: int = Field(ge=0)
    provider_attempt_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    failures: tuple[ReceiptFailureTaxonomyEntryV3, ...] = ()

    @model_validator(mode="after")
    def validate_judge_accounting(self) -> JudgeAccountingV3:
        total_jobs = self.development_job_count + self.heldout_job_count
        if (
            self.completed_job_count + self.failed_job_count + self.ambiguous_job_count
            != total_jobs
        ):
            raise ValueError("judge accounting does not cover every job")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("judge accounting token totals do not reconcile")
        if self.retry_count > self.provider_attempt_count:
            raise ValueError("judge retries exceed provider attempts")
        codes = tuple(item.safe_error_code for item in self.failures)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("judge failure taxonomy must be sorted and unique")
        if sum(item.receipt_count for item in self.failures) != (
            self.failed_job_count + self.ambiguous_job_count
        ):
            raise ValueError("judge failure taxonomy does not cover terminal failures")
        return self

    @property
    def effective_cost_usd(self) -> Decimal:
        return self.known_cost_usd + self.unresolved_reserved_cost_usd


def _empty_judge_accounting_v3() -> JudgeAccountingV3:
    return JudgeAccountingV3(
        development_job_count=0,
        heldout_job_count=0,
        completed_job_count=0,
        failed_job_count=0,
        ambiguous_job_count=0,
        known_cost_usd=Decimal("0"),
        unresolved_reserved_cost_usd=Decimal("0"),
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        generation_call_count=0,
        provider_attempt_count=0,
        retry_count=0,
        failures=(),
    )


class JudgedObservationSetV3(FrozenBenchmarkReportingContractV3):
    """Observations produced only after a complete model-judge join."""

    bindings: ArtifactBindingsV3
    judge_configuration_sha256: str = Field(pattern=_SHA256)
    calibration_sha256: str = Field(pattern=_SHA256)
    observations: tuple[EvaluationObservationV3, ...] = Field(min_length=1)
    authoritative_observation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_observations(self) -> JudgedObservationSetV3:
        observation_ids = tuple(item.observation_id for item in self.observations)
        execution_orders = tuple(item.execution_order for item in self.observations)
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("judged observation set contains duplicate observations")
        if len(execution_orders) != len(set(execution_orders)):
            raise ValueError("judged observation set reuses execution orders")
        if any(item.status is not TaskStatus.SUCCESS for item in self.observations):
            raise ValueError(
                "judged observations must have successful execution status"
            )
        authoritative_ids = self.authoritative_observation_ids
        if authoritative_ids != tuple(sorted(set(authoritative_ids))):
            raise ValueError("authoritative observation IDs must be sorted and unique")
        if not set(authoritative_ids).issubset(observation_ids):
            raise ValueError(
                "authoritative observation IDs are outside the judgment set"
            )
        return self


class Package8ResultsSummaryV3(FrozenBenchmarkReportingContractV3):
    """Portable P8 accounting, including SUT and automated-judge usage."""

    schema_version: str = "8.0"
    run_id: str = Field(pattern=_V3_IDENTIFIER)
    completion_status: str = Field(pattern=r"^(partial|complete)$")
    analysis_sha256: str = Field(pattern=_SHA256)
    report_sha256: str = Field(pattern=_SHA256)
    accepted_observation_count: int = Field(ge=0)
    expected_observation_count: int = Field(ge=1)
    known_cost_usd: Decimal = Field(ge=0)
    unresolved_reserved_cost_usd: Decimal = Field(ge=0)
    effective_cost_usd: Decimal = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    generation_call_count: int = Field(ge=0)
    embedding_call_count: int = Field(ge=0)
    provider_attempt_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    failure_taxonomy: tuple[ReceiptFailureTaxonomyEntryV3, ...] = ()
    judge_accounting: JudgeAccountingV3 = Field(
        default_factory=_empty_judge_accounting_v3
    )
    total_known_cost_usd: Decimal = Field(ge=0)
    total_unresolved_reserved_cost_usd: Decimal = Field(ge=0)
    total_effective_cost_usd: Decimal = Field(ge=0)
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    aggregate_total_tokens: int = Field(ge=0)
    total_generation_call_count: int = Field(ge=0)
    total_embedding_call_count: int = Field(ge=0)
    total_provider_attempt_count: int = Field(ge=0)
    total_retry_count: int = Field(ge=0)
    document_recall_authority: str = "resolved_turn_result_citations_only"
    document_recall_authority_count: int = Field(ge=0)
    citationless_observation_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> Package8ResultsSummaryV3:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Package 8 summary token totals do not reconcile")
        if self.effective_cost_usd != (
            self.known_cost_usd + self.unresolved_reserved_cost_usd
        ):
            raise ValueError("Package 8 summary effective cost does not reconcile")
        judge = self.judge_accounting
        if self.total_known_cost_usd != self.known_cost_usd + judge.known_cost_usd:
            raise ValueError("Package 8 total known cost does not reconcile")
        if self.total_unresolved_reserved_cost_usd != (
            self.unresolved_reserved_cost_usd + judge.unresolved_reserved_cost_usd
        ):
            raise ValueError("Package 8 total unresolved cost does not reconcile")
        if self.total_effective_cost_usd != (
            self.total_known_cost_usd + self.total_unresolved_reserved_cost_usd
        ):
            raise ValueError("Package 8 total effective cost does not reconcile")
        if self.total_input_tokens != self.input_tokens + judge.input_tokens:
            raise ValueError("Package 8 total input tokens do not reconcile")
        if self.total_output_tokens != self.output_tokens + judge.output_tokens:
            raise ValueError("Package 8 total output tokens do not reconcile")
        if (
            self.aggregate_total_tokens
            != self.total_input_tokens + self.total_output_tokens
        ):
            raise ValueError("Package 8 total tokens do not reconcile")
        if self.total_generation_call_count != (
            self.generation_call_count + judge.generation_call_count
        ):
            raise ValueError("Package 8 total generation calls do not reconcile")
        if self.total_embedding_call_count != self.embedding_call_count:
            raise ValueError("Package 8 total embedding calls do not reconcile")
        if self.total_provider_attempt_count != (
            self.provider_attempt_count + judge.provider_attempt_count
        ):
            raise ValueError("Package 8 total provider attempts do not reconcile")
        if self.total_retry_count != self.retry_count + judge.retry_count:
            raise ValueError("Package 8 total retries do not reconcile")
        if (
            self.document_recall_authority_count + self.citationless_observation_count
            != self.accepted_observation_count
        ):
            raise ValueError(
                "citation authority accounting does not cover accepted observations"
            )
        return self


class Package8AnalysisResultV3(FrozenBenchmarkReportingContractV3):
    analysis: EvaluationAnalysisV3
    report_sha256: str = Field(pattern=_SHA256)


def convert_completed_receipts_to_provisionals_v3(
    receipts: Sequence[ObservationRunReceiptV3],
    *,
    admission: HeldOutReceiptAdmissionV3,
    evidence_resolver: ImmutableEvidenceResolverV3,
) -> tuple[ProvisionalObservationV3, ...]:
    """Convert an admitted, completed held-out receipt batch without scoring it.

    Every batch-level identity and ledger invariant is checked before an evidence
    resolver is invoked, so a structurally invalid receipt can never produce a
    partially trusted blind-answer input.
    """

    materialized_admission = _materialize_admission(admission)
    checked: list[tuple[ObservationRunReceiptV3, TurnResult]] = []
    observation_ids: set[str] = set()
    execution_orders: set[int] = set()
    ledger_event_ids: set[str] = set()
    for raw_receipt in receipts:
        receipt, final_result = _validate_receipt_for_conversion(
            raw_receipt,
            admission=materialized_admission,
        )
        assert receipt.observation_id is not None
        assert receipt.execution_order is not None
        if receipt.observation_id in observation_ids:
            raise BenchmarkReportingValidationErrorV3(
                "receipt batch contains a duplicate observation ID"
            )
        if receipt.execution_order in execution_orders:
            raise BenchmarkReportingValidationErrorV3(
                "receipt batch reuses an execution order"
            )
        receipt_ledger_ids = tuple(
            event.ledger_event_id
            for result in receipt.turn_results
            for event in result.ledger_events
        )
        if ledger_event_ids.intersection(receipt_ledger_ids):
            raise BenchmarkReportingValidationErrorV3(
                "receipt batch reuses a ledger event ID"
            )
        observation_ids.add(receipt.observation_id)
        execution_orders.add(receipt.execution_order)
        ledger_event_ids.update(receipt_ledger_ids)
        checked.append((receipt, final_result))

    return tuple(
        _provisional_from_receipt(
            receipt,
            final_result=final_result,
            evidence_resolver=evidence_resolver,
        )
        for receipt, final_result in sorted(
            checked,
            key=lambda item: (
                item[0].execution_order or -1,
                item[0].observation_id or "",
            ),
        )
    )


def account_observation_receipts_v3(
    receipts: Sequence[ObservationRunReceiptV3],
) -> ReceiptAccountingV3:
    """Account for validated terminal receipts without treating failures as results."""

    materialized = tuple(_materialize_receipt(item) for item in receipts)
    seen_turn_ids: set[str] = set()
    seen_ledger_ids: set[str] = set()
    failure_codes: Counter[str] = Counter()
    known_cost = Decimal("0")
    unresolved_cost = Decimal("0")
    input_tokens = output_tokens = 0
    generation_calls = embedding_calls = provider_attempts = retries = 0
    completed_measured = failed = warmups = 0
    for receipt in materialized:
        if receipt.canonical_turn_id in seen_turn_ids:
            raise BenchmarkReportingValidationErrorV3(
                "receipt accounting contains a duplicate canonical turn ID"
            )
        seen_turn_ids.add(receipt.canonical_turn_id)
        ledger_ids = tuple(
            event.ledger_event_id
            for result in receipt.turn_results
            for event in result.ledger_events
        )
        if seen_ledger_ids.intersection(ledger_ids):
            raise BenchmarkReportingValidationErrorV3(
                "receipt accounting reuses a ledger event ID"
            )
        seen_ledger_ids.update(ledger_ids)
        known_cost += receipt.totals.known_cost_usd
        unresolved_cost += receipt.totals.unresolved_reserved_cost_usd
        input_tokens += receipt.totals.input_tokens
        output_tokens += receipt.totals.output_tokens
        generation_calls += receipt.totals.generation_calls
        provider_attempts += receipt.totals.provider_attempts
        retries += receipt.totals.retry_count
        embedding_calls += sum(
            len(result.embedding_calls) for result in receipt.turn_results
        )
        if receipt.identity.turn_kind is ScheduledTurnKindV3.WARMUP:
            warmups += 1
        if receipt.terminal_status is ObservationTerminalStatusV3.COMPLETED:
            if receipt.identity.turn_kind is ScheduledTurnKindV3.MEASURED:
                completed_measured += 1
        else:
            failed += 1
            assert receipt.safe_error_code is not None
            failure_codes[receipt.safe_error_code] += 1
    return ReceiptAccountingV3(
        receipt_count=len(materialized),
        completed_measured_receipt_count=completed_measured,
        failed_receipt_count=failed,
        warmup_receipt_count=warmups,
        known_cost_usd=known_cost,
        unresolved_reserved_cost_usd=unresolved_cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        generation_call_count=generation_calls,
        embedding_call_count=embedding_calls,
        provider_attempt_count=provider_attempts,
        retry_count=retries,
        failures=tuple(
            ReceiptFailureTaxonomyEntryV3(
                safe_error_code=code,
                receipt_count=count,
            )
            for code, count in sorted(failure_codes.items())
        ),
    )


def join_model_judgments_to_observations_v3(
    provisionals: Sequence[ProvisionalObservationV3],
    *,
    blinded_packet: BlindedAnswerPacketV3,
    unblinding_key: UnblindingKeyV3,
    judgments: Sequence[JudgmentRecordV3],
    judge_configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
) -> JudgedObservationSetV3:
    """Unblind and apply one complete, consistently calibrated model-judge set."""

    packet = _materialize_packet(blinded_packet)
    key = _materialize_key(unblinding_key)
    configuration = _materialize_configuration(judge_configuration)
    frozen_calibration = _materialize_calibration(calibration)
    try:
        BlindedAnswerArtifactsV3(packet=packet, unblinding_key=key)
    except ValueError as exc:
        raise BenchmarkReportingValidationErrorV3(
            "blind packet and unblinding key are invalid"
        ) from exc
    if packet.bindings != configuration.bindings:
        raise BenchmarkReportingValidationErrorV3(
            "judge configuration bindings differ from the blind packet"
        )
    if frozen_calibration.protocol_sha256 != packet.bindings.protocol_sha256:
        raise BenchmarkReportingValidationErrorV3(
            "calibration protocol binding differs from the blind packet"
        )
    if frozen_calibration.configuration_sha256 != configuration.configuration_sha256:
        raise BenchmarkReportingValidationErrorV3(
            "calibration configuration binding differs from the model judge"
        )

    normalized = tuple(_materialize_provisional(item) for item in provisionals)
    by_observation = {item.observation.observation_id: item for item in normalized}
    if len(by_observation) != len(normalized):
        raise BenchmarkReportingValidationErrorV3(
            "provisional observations contain duplicate observation IDs"
        )
    packet_by_opaque = {item.opaque_answer_id: item for item in packet.answers}
    key_by_opaque = {item.opaque_answer_id: item for item in key.entries}
    if len(key_by_opaque) != len(key.entries):
        raise BenchmarkReportingValidationErrorV3(
            "unblinding key contains duplicate answers"
        )
    if set(packet_by_opaque) != set(key_by_opaque):
        raise BenchmarkReportingValidationErrorV3(
            "blind packet and unblinding key answer IDs differ"
        )
    if {entry.observation_id for entry in key.entries} != set(by_observation):
        raise BenchmarkReportingValidationErrorV3(
            "unblinding key does not exactly cover provisional observations"
        )

    for opaque_id, entry in key_by_opaque.items():
        provisional = by_observation.get(entry.observation_id)
        if provisional is None:
            raise BenchmarkReportingValidationErrorV3(
                "unblinding key references an unknown observation"
            )
        answer = packet_by_opaque[opaque_id]
        if (
            entry.variant_id,
            entry.conversation_id,
            entry.work_group_id,
            entry.repetition,
            answer.answer,
            answer.citations,
        ) != (
            provisional.observation.variant_id,
            provisional.observation.case_id,
            provisional.observation.work_group_id,
            provisional.observation.repetition,
            provisional.answer_evidence.answer,
            provisional.answer_evidence.citations,
        ):
            raise BenchmarkReportingValidationErrorV3(
                "blind packet or unblinding entry differs from receipt-derived evidence"
            )

    normalized_judgments = tuple(_materialize_judgment(item) for item in judgments)
    by_judgment = {item.opaque_answer_id: item for item in normalized_judgments}
    if len(by_judgment) != len(normalized_judgments):
        raise BenchmarkReportingValidationErrorV3(
            "model judge records contain duplicate opaque answer IDs"
        )
    if set(by_judgment) != set(packet_by_opaque):
        raise BenchmarkReportingValidationErrorV3(
            "model judge records must cover every and only blind answers"
        )
    for judgment in normalized_judgments:
        _validate_model_judgment(
            judgment,
            packet=packet,
            configuration=configuration,
            calibration=frozen_calibration,
        )

    joined: list[EvaluationObservationV3] = []
    authoritative_observation_ids: list[str] = []
    for opaque_id, entry in key_by_opaque.items():
        provisional = by_observation[entry.observation_id]
        if provisional.authoritative_evidence:
            authoritative_observation_ids.append(entry.observation_id)
        joined.append(_apply_judgment(provisional, by_judgment[opaque_id]))
    return JudgedObservationSetV3(
        bindings=packet.bindings,
        judge_configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=frozen_calibration.calibration_sha256,
        observations=tuple(sorted(joined, key=lambda item: item.execution_order)),
        authoritative_observation_ids=tuple(sorted(authoritative_observation_ids)),
    )


def analyze_judged_observations_v3(
    judged: JudgedObservationSetV3,
    *,
    protocol: EvaluationProtocolV3,
    split: EvaluationSplitManifestV3,
    repeat_decision: RepeatDecisionV3,
    run_id: str,
    gold_sha256: str,
    split_sha256: str,
    schedule_sha256: str,
    bootstrap_samples: int = 10_000,
) -> Package8AnalysisResultV3:
    """Call the immutable v3 analyzer only after the model-judge join succeeded."""

    normalized = JudgedObservationSetV3.model_validate(judged.model_dump(mode="json"))
    analysis = analyze_evaluation_v3(
        protocol,
        split,
        repeat_decision,
        normalized.observations,
        run_id=run_id,
        gold_sha256=gold_sha256,
        split_sha256=split_sha256,
        schedule_sha256=schedule_sha256,
        bootstrap_samples=bootstrap_samples,
    )
    report = build_evaluation_report_v3(analysis)
    return Package8AnalysisResultV3(
        analysis=analysis, report_sha256=report.report_sha256
    )


def build_package8_results_summary_v3(
    *,
    accounting: ReceiptAccountingV3,
    judged: JudgedObservationSetV3,
    analysis: EvaluationAnalysisV3,
    report_sha256: str,
    judge_accounting: JudgeAccountingV3 | None = None,
) -> Package8ResultsSummaryV3:
    """Build a JSON-serializable accounting summary without concealing partiality."""

    normalized_accounting = ReceiptAccountingV3.model_validate(
        accounting.model_dump(mode="json")
    )
    normalized_judged = JudgedObservationSetV3.model_validate(
        judged.model_dump(mode="json")
    )
    normalized_analysis = EvaluationAnalysisV3.model_validate(
        analysis.model_dump(mode="json")
    )
    normalized_judge = JudgeAccountingV3.model_validate(
        (
            _empty_judge_accounting_v3()
            if judge_accounting is None
            else judge_accounting
        ).model_dump(mode="json")
    )
    if normalized_analysis.bindings != normalized_judged.bindings:
        raise BenchmarkReportingValidationErrorV3(
            "analysis bindings differ from the judged observation set"
        )
    if normalized_analysis.accepted_observation_count != len(
        normalized_judged.observations
    ):
        raise BenchmarkReportingValidationErrorV3(
            "analysis accepted count differs from the judged observation set"
        )
    if len({item.observation_id for item in normalized_judged.observations}) != len(
        normalized_judged.observations
    ):
        raise BenchmarkReportingValidationErrorV3(
            "judged observations are not uniquely accountable"
        )
    return Package8ResultsSummaryV3(
        run_id=normalized_analysis.run_id,
        completion_status=normalized_analysis.completion_status,
        analysis_sha256=normalized_analysis.analysis_sha256,
        report_sha256=report_sha256,
        accepted_observation_count=normalized_analysis.accepted_observation_count,
        expected_observation_count=normalized_analysis.expected_observation_count,
        known_cost_usd=normalized_accounting.known_cost_usd,
        unresolved_reserved_cost_usd=(
            normalized_accounting.unresolved_reserved_cost_usd
        ),
        effective_cost_usd=normalized_accounting.effective_cost_usd,
        input_tokens=normalized_accounting.input_tokens,
        output_tokens=normalized_accounting.output_tokens,
        total_tokens=normalized_accounting.total_tokens,
        generation_call_count=normalized_accounting.generation_call_count,
        embedding_call_count=normalized_accounting.embedding_call_count,
        provider_attempt_count=normalized_accounting.provider_attempt_count,
        retry_count=normalized_accounting.retry_count,
        failure_taxonomy=normalized_accounting.failures,
        judge_accounting=normalized_judge,
        total_known_cost_usd=(
            normalized_accounting.known_cost_usd + normalized_judge.known_cost_usd
        ),
        total_unresolved_reserved_cost_usd=(
            normalized_accounting.unresolved_reserved_cost_usd
            + normalized_judge.unresolved_reserved_cost_usd
        ),
        total_effective_cost_usd=(
            normalized_accounting.effective_cost_usd
            + normalized_judge.effective_cost_usd
        ),
        total_input_tokens=(
            normalized_accounting.input_tokens + normalized_judge.input_tokens
        ),
        total_output_tokens=(
            normalized_accounting.output_tokens + normalized_judge.output_tokens
        ),
        aggregate_total_tokens=(
            normalized_accounting.total_tokens + normalized_judge.total_tokens
        ),
        total_generation_call_count=(
            normalized_accounting.generation_call_count
            + normalized_judge.generation_call_count
        ),
        total_embedding_call_count=normalized_accounting.embedding_call_count,
        total_provider_attempt_count=(
            normalized_accounting.provider_attempt_count
            + normalized_judge.provider_attempt_count
        ),
        total_retry_count=(
            normalized_accounting.retry_count + normalized_judge.retry_count
        ),
        document_recall_authority_count=len(
            normalized_judged.authoritative_observation_ids
        ),
        citationless_observation_count=(
            len(normalized_judged.observations)
            - len(normalized_judged.authoritative_observation_ids)
        ),
    )


def _validate_receipt_for_conversion(
    raw_receipt: ObservationRunReceiptV3,
    *,
    admission: HeldOutReceiptAdmissionV3,
) -> tuple[ObservationRunReceiptV3, TurnResult]:
    receipt = _materialize_receipt(raw_receipt)
    if receipt.terminal_status is not ObservationTerminalStatusV3.COMPLETED:
        raise BenchmarkReportingValidationErrorV3(
            "only completed observation receipts can enter benchmark reporting"
        )
    if receipt.identity.turn_kind is not ScheduledTurnKindV3.MEASURED:
        raise BenchmarkReportingValidationErrorV3(
            "warmup receipts cannot enter held-out benchmark reporting"
        )
    if receipt.execution_order is None or receipt.observation_id is None:
        raise BenchmarkReportingValidationErrorV3(
            "measured receipt is missing its execution or observation identity"
        )
    if (
        receipt.run_id != admission.run_id
        or receipt.protocol_sha256 != admission.protocol_sha256
        or receipt.schedule_sha256 != admission.schedule_sha256
        or receipt.observation_id not in admission.expected_observation_ids
    ):
        raise BenchmarkReportingValidationErrorV3(
            "receipt identity is foreign to the held-out reporting admission"
        )
    pilot_cost = receipt.pilot_cost
    if (
        pilot_cost is None
        or not pilot_cost.ledger_attributed
        or not pilot_cost.ledger_valid
    ):
        raise BenchmarkReportingValidationErrorV3(
            "completed receipt lacks valid authoritative ledger attribution"
        )
    ledger_ids = tuple(
        event.ledger_event_id
        for result in receipt.turn_results
        for event in result.ledger_events
    )
    if not ledger_ids or len(ledger_ids) != len(set(ledger_ids)):
        raise BenchmarkReportingValidationErrorV3(
            "receipt has missing or reused ledger evidence"
        )
    if pilot_cost.ledger_event_ids != ledger_ids:
        raise BenchmarkReportingValidationErrorV3(
            "receipt pilot ledger IDs differ from the actual ledger evidence"
        )
    return receipt, _final_turn_result(receipt)


def _provisional_from_receipt(
    receipt: ObservationRunReceiptV3,
    *,
    final_result: TurnResult,
    evidence_resolver: ImmutableEvidenceResolverV3,
) -> ProvisionalObservationV3:
    assert receipt.observation_id is not None
    assert receipt.execution_order is not None
    operational = _operational_evidence(receipt)
    citations, authoritative_evidence = _extract_citations(
        final_result,
        evidence_resolver=evidence_resolver,
    )
    abstained = final_result.outcome is DialogueOutcome.ABSTAINED
    observation = EvaluationObservationV3(
        observation_id=receipt.observation_id,
        identity=receipt.identity,
        run_id=receipt.run_id,
        protocol_sha256=receipt.protocol_sha256,
        variant_id=receipt.identity.variant_id,
        case_id=receipt.identity.case_id,
        work_group_id=receipt.identity.work_group_id,
        repetition=receipt.identity.repetition,
        execution_order=receipt.execution_order,
        status=TaskStatus.PARTIAL_SUCCESS,
        task_completed=False,
        answerable=not abstained,
        abstained=abstained,
        claim_support=None,
        citation_precision=None,
        citation_coverage=None,
        document_recall=None,
        authorized=False,
        valid_plan=False,
        useful_continuation=None,
        duplicate_dispatch_count=0,
        end_to_end_latency_ms=operational.end_to_end_latency_ms,
        input_tokens=operational.input_tokens,
        output_tokens=operational.output_tokens,
        total_tokens=operational.total_tokens,
        known_cost_usd=operational.known_cost_usd,
        unresolved_reserved_cost_usd=operational.unresolved_reserved_cost_usd,
        ledger_event_ids=operational.ledger_event_ids,
    )
    return ProvisionalObservationV3(
        observation=observation,
        answer_evidence=AnswerEvidenceInputV3(
            observation_id=observation.observation_id,
            variant_id=observation.variant_id,
            conversation_id=observation.case_id,
            work_group_id=observation.work_group_id,
            repetition=observation.repetition,
            answer=final_result.answer,
            citations=citations,
        ),
        final_turn_result=final_result,
        authoritative_evidence=authoritative_evidence,
        operational=operational,
    )


def _operational_evidence(
    receipt: ObservationRunReceiptV3,
) -> ReceiptOperationalEvidenceV3:
    return ReceiptOperationalEvidenceV3(
        canonical_turn_id=receipt.canonical_turn_id,
        ledger_event_ids=tuple(
            event.ledger_event_id
            for result in receipt.turn_results
            for event in result.ledger_events
        ),
        known_cost_usd=receipt.totals.known_cost_usd,
        unresolved_reserved_cost_usd=(receipt.totals.unresolved_reserved_cost_usd),
        generation_call_count=receipt.totals.generation_calls,
        embedding_call_count=sum(
            len(result.embedding_calls) for result in receipt.turn_results
        ),
        provider_attempt_count=receipt.totals.provider_attempts,
        retry_count=receipt.totals.retry_count,
        input_tokens=receipt.totals.input_tokens,
        output_tokens=receipt.totals.output_tokens,
        total_tokens=receipt.totals.input_tokens + receipt.totals.output_tokens,
        end_to_end_latency_ms=(
            sum(result.elapsed_seconds for result in receipt.turn_results) * 1_000
        ),
    )


def _final_turn_result(receipt: ObservationRunReceiptV3) -> TurnResult:
    if not receipt.turn_results:
        raise BenchmarkReportingValidationErrorV3(
            "completed receipt has no user-turn result to report"
        )
    try:
        outcome = DurableTurnOutcome.model_validate(
            receipt.turn_results[-1].result_payload
        )
    except (TypeError, ValueError) as exc:
        raise BenchmarkReportingValidationErrorV3(
            "receipt final result payload is not a valid durable turn outcome"
        ) from exc
    if outcome.status is not TurnStatus.COMPLETED or outcome.result is None:
        raise BenchmarkReportingValidationErrorV3(
            "receipt final result payload does not contain a completed TurnResult"
        )
    return outcome.result


def _extract_citations(
    result: TurnResult,
    *,
    evidence_resolver: ImmutableEvidenceResolverV3,
) -> tuple[tuple[CitationForReviewV3, ...], tuple[EvidenceBindingKeyV3, ...]]:
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    citations: list[CitationForReviewV3] = []
    bindings: list[EvidenceBindingKeyV3] = []
    labels: set[str] = set()
    for citation in result.citations:
        evidence = evidence_by_id.get(citation.evidence_id)
        if evidence is None:
            raise BenchmarkReportingValidationErrorV3(
                "TurnResult citation references missing evidence"
            )
        if citation.span_id != evidence.span_id:
            raise BenchmarkReportingValidationErrorV3(
                "TurnResult citation span differs from its immutable evidence binding"
            )
        binding = EvidenceBindingKeyV3(
            evidence_id=evidence.evidence_id,
            source_id=evidence.source_id,
            source_version_id=evidence.source_version_id,
            chunk_id=evidence.chunk_id,
            span_id=evidence.span_id,
        )
        try:
            resolved = evidence_resolver.resolve(binding)
        except Exception as exc:
            raise BenchmarkReportingValidationErrorV3(
                "immutable evidence resolver failed for a cited source"
            ) from exc
        if not isinstance(resolved, ResolvedExactEvidenceV3):
            raise BenchmarkReportingValidationErrorV3(
                "immutable evidence resolver did not return exact evidence"
            )
        try:
            resolved = ResolvedExactEvidenceV3.model_validate(
                resolved.model_dump(mode="json")
            )
        except ValueError as exc:
            raise BenchmarkReportingValidationErrorV3(
                "immutable evidence resolver returned invalid exact evidence"
            ) from exc
        if resolved.binding != binding:
            raise BenchmarkReportingValidationErrorV3(
                "immutable evidence resolver returned a foreign evidence binding"
            )
        if citation.display_label in labels:
            raise BenchmarkReportingValidationErrorV3(
                "TurnResult citations reuse a display label"
            )
        labels.add(citation.display_label)
        citations.append(
            CitationForReviewV3(
                label=citation.display_label,
                evidence=resolved.exact_text,
            )
        )
        bindings.append(binding)
    return tuple(citations), tuple(dict.fromkeys(bindings))


def _validate_model_judgment(
    judgment: JudgmentRecordV3,
    *,
    packet: BlindedAnswerPacketV3,
    configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
) -> None:
    if judgment.bindings != packet.bindings:
        raise BenchmarkReportingValidationErrorV3(
            "model judgment bindings differ from the blind packet"
        )
    if judgment.blinded_packet_sha256 != packet.packet_sha256:
        raise BenchmarkReportingValidationErrorV3(
            "model judgment references a different blind packet"
        )
    if judgment.judgment_mode.value != "model_judge":
        raise BenchmarkReportingValidationErrorV3(
            "held-out reporting requires declared model-judge records"
        )
    if judgment.model_binding != configuration.model_binding:
        raise BenchmarkReportingValidationErrorV3(
            "model judgment binding differs from the declared judge model"
        )
    if (
        judgment.judge_configuration_sha256 != configuration.configuration_sha256
        or judgment.calibration_sha256 != calibration.calibration_sha256
        or judgment.judge_output_sha256 is None
    ):
        raise BenchmarkReportingValidationErrorV3(
            "model judgment calibration or configuration binding is incomplete"
        )
    if set(judgment.scores) != set(COMPLETE_JUDGED_METRICS_V3):
        raise BenchmarkReportingValidationErrorV3(
            "model judgment does not contain the complete required metric set"
        )
    expected_sources = {
        **{metric: "model_judge" for metric in SEMANTIC_JUDGE_METRICS_V3},
        **{metric: "deterministic" for metric in DETERMINISTIC_JUDGE_METRICS_V3},
    }
    if judgment.score_sources != expected_sources:
        raise BenchmarkReportingValidationErrorV3(
            "model judgment score sources do not match the frozen metric contract"
        )
    boolean_metrics = (
        EvaluationMetricV3.TASK_COMPLETION,
        EvaluationMetricV3.ANSWERABILITY_ABSTENTION,
        EvaluationMetricV3.AUTHORIZATION,
        EvaluationMetricV3.VALID_PLAN,
        EvaluationMetricV3.USEFUL_CONTINUATION,
    )
    if any(judgment.scores[metric] not in {0.0, 1.0} for metric in boolean_metrics):
        raise BenchmarkReportingValidationErrorV3(
            "boolean model-judge metrics must be exact zero or one"
        )


def _apply_judgment(
    provisional: ProvisionalObservationV3,
    judgment: JudgmentRecordV3,
) -> EvaluationObservationV3:
    scores = judgment.scores
    abstained = provisional.final_turn_result.outcome is DialogueOutcome.ABSTAINED
    answerability_correct = _as_bool(
        scores[EvaluationMetricV3.ANSWERABILITY_ABSTENTION]
    )
    payload = provisional.observation.model_dump(mode="json")
    payload.update(
        {
            "status": TaskStatus.SUCCESS,
            "task_completed": _as_bool(scores[EvaluationMetricV3.TASK_COMPLETION]),
            # The receipt establishes whether the model abstained.  The judge's
            # binary score establishes whether that action matched answerability.
            "answerable": (not abstained if answerability_correct else abstained),
            "abstained": abstained,
            "claim_support": scores[EvaluationMetricV3.CLAIM_SUPPORT],
            "citation_precision": scores[EvaluationMetricV3.CITATION_PRECISION],
            "citation_coverage": scores[EvaluationMetricV3.CITATION_COVERAGE],
            "document_recall": scores[EvaluationMetricV3.DOCUMENT_RECALL],
            "authorized": _as_bool(scores[EvaluationMetricV3.AUTHORIZATION]),
            "valid_plan": _as_bool(scores[EvaluationMetricV3.VALID_PLAN]),
            "useful_continuation": _as_bool(
                scores[EvaluationMetricV3.USEFUL_CONTINUATION]
            ),
        }
    )
    return EvaluationObservationV3.model_validate(payload)


def _as_bool(score: float) -> bool:
    if score not in {0.0, 1.0}:
        raise BenchmarkReportingValidationErrorV3(
            "a boolean metric score must be exactly zero or one"
        )
    return bool(score)


def _materialize_receipt(value: ObservationRunReceiptV3) -> ObservationRunReceiptV3:
    if not isinstance(value, ObservationRunReceiptV3):
        raise BenchmarkReportingValidationErrorV3(
            "benchmark reporting accepts only ObservationRunReceiptV3 records"
        )
    try:
        return ObservationRunReceiptV3.model_validate(value.model_dump(mode="json"))
    except ValueError as exc:
        raise BenchmarkReportingValidationErrorV3(
            "receipt failed immutable validation"
        ) from exc


def _materialize_admission(
    value: HeldOutReceiptAdmissionV3,
) -> HeldOutReceiptAdmissionV3:
    if not isinstance(value, HeldOutReceiptAdmissionV3):
        raise BenchmarkReportingValidationErrorV3(
            "receipt admission contract is invalid"
        )
    return HeldOutReceiptAdmissionV3.model_validate(value.model_dump(mode="json"))


def _materialize_provisional(
    value: ProvisionalObservationV3,
) -> ProvisionalObservationV3:
    if not isinstance(value, ProvisionalObservationV3):
        raise BenchmarkReportingValidationErrorV3(
            "provisional observation contract is invalid"
        )
    return ProvisionalObservationV3.model_validate(value.model_dump(mode="json"))


def _materialize_packet(value: BlindedAnswerPacketV3) -> BlindedAnswerPacketV3:
    if not isinstance(value, BlindedAnswerPacketV3):
        raise BenchmarkReportingValidationErrorV3("blinded answer packet is invalid")
    return BlindedAnswerPacketV3.model_validate(value.model_dump(mode="json"))


def _materialize_key(value: UnblindingKeyV3) -> UnblindingKeyV3:
    if not isinstance(value, UnblindingKeyV3):
        raise BenchmarkReportingValidationErrorV3("unblinding key is invalid")
    return UnblindingKeyV3.model_validate(value.model_dump(mode="json"))


def _materialize_judgment(value: JudgmentRecordV3) -> JudgmentRecordV3:
    if not isinstance(value, JudgmentRecordV3):
        raise BenchmarkReportingValidationErrorV3("judgment record is invalid")
    try:
        return JudgmentRecordV3.model_validate(value.model_dump(mode="json"))
    except ValidationError as exc:
        raise BenchmarkReportingValidationErrorV3("judgment record is invalid") from exc


def _materialize_configuration(
    value: ModelJudgeConfigurationV3,
) -> ModelJudgeConfigurationV3:
    if not isinstance(value, ModelJudgeConfigurationV3):
        raise BenchmarkReportingValidationErrorV3("judge configuration is invalid")
    return ModelJudgeConfigurationV3.model_validate(value.model_dump(mode="json"))


def _materialize_calibration(value: CalibrationFreezeV3) -> CalibrationFreezeV3:
    if not isinstance(value, CalibrationFreezeV3):
        raise BenchmarkReportingValidationErrorV3("calibration freeze is invalid")
    return CalibrationFreezeV3.model_validate(value.model_dump(mode="json"))
