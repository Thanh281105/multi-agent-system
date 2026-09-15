"""Local-only assembly for the additive Package 8 benchmark artifacts.

This module deliberately contains no provider client.  It converts durable SUT
receipts into a blinded packet, then accepts only already-terminal model-judge
records for the final analysis.  Dispatch and recovery stay in the dedicated
held-out and judge runners.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.benchmark_judging import (
    JudgeJobResultV3,
    build_development_judge_run_key_v3,
    build_heldout_judge_run_key_v3,
)
from app.evaluation.benchmark_reporting import (
    BenchmarkReportingValidationErrorV3,
    HeldOutReceiptAdmissionV3,
    ImmutableEvidenceResolverV3,
    JudgeAccountingV3,
    Package8ResultsSummaryV3,
    ProvisionalObservationV3,
    ReceiptAccountingV3,
    account_observation_receipts_v3,
    analyze_judged_observations_v3,
    build_package8_results_summary_v3,
    convert_completed_receipts_to_provisionals_v3,
    join_model_judgments_to_observations_v3,
)
from app.evaluation.benchmark_v3 import (
    FrozenPackage7HeldoutInputsV3,
    validate_heldout_schedule_v3,
)
from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerArtifactsV3,
    JudgmentRecordV3,
    build_blinded_answer_packet_v3,
    build_evaluation_report_v3,
    write_evaluation_artifacts_v3,
)
from app.evaluation.v3_comparison import EvaluationAnalysisV3, analyze_evaluation_v3
from app.evaluation.v3_judge import (
    CalibrationFreezeV3,
    CalibrationRecordV3,
    CalibrationReferenceBundleV3,
    CalibrationThresholdsV3,
    ModelJudgeConfigurationV3,
    blinded_answer_sha256_v3,
)
from app.evaluation.v3_models import ScheduledTurnV3
from app.evaluation.v3_runner import ObservationRunReceiptV3
from app.evaluation.v3_schedule import pilot_schedule_sha256_v3

_SHA256 = r"^[a-f0-9]{64}$"
_PREPARATION_SCHEMA = "8.0"
_PREPARATION_SEED_MASK = 0x8A17_C2D9
_NANO_USD = Decimal(1_000_000_000)
_POSTPROCESSING_SOURCE_NAMES = (
    "benchmark_cli.py",
    "benchmark_v3.py",
    "benchmark_evidence.py",
    "benchmark_reporting.py",
    "benchmark_judging.py",
    "benchmark_finalization.py",
)


class Package8FinalizationErrorV3(RuntimeError):
    """A local Package 8 evidence or artifact invariant failed."""


class FrozenPackage8FinalizationV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def package8_postprocessing_source_sha256_v3() -> str:
    """Hash every additive module that can affect a P8 judgment or report."""

    root = Path(__file__).resolve().parent
    payload: list[dict[str, str]] = []
    for name in _POSTPROCESSING_SOURCE_NAMES:
        path = root / name
        if not path.is_file():
            raise Package8FinalizationErrorV3("package8_postprocessing_source_missing")
        content = path.read_bytes().replace(b"\r\n", b"\n")
        payload.append(
            {
                "path": f"app/evaluation/{name}",
                "sha256": canonical_sha256({"content": content.hex()}),
            }
        )
    return canonical_sha256(payload)


class Package8JudgingPreparationV3(FrozenPackage8FinalizationV3):
    """Write-once candidate inputs for blinded held-out judgment."""

    schema_version: str = _PREPARATION_SCHEMA
    run_id: str
    protocol_sha256: str = Field(pattern=_SHA256)
    schedule_sha256: str = Field(pattern=_SHA256)
    postprocessing_source_manifest_sha256: str = Field(pattern=_SHA256)
    receipt_accounting: ReceiptAccountingV3
    admission: HeldOutReceiptAdmissionV3
    provisionals: tuple[ProvisionalObservationV3, ...]
    pre_judgment_analysis: EvaluationAnalysisV3
    blinded: BlindedAnswerArtifactsV3
    preparation_sha256: str = Field(pattern=_SHA256)

    def model_post_init(self, __context: object) -> None:
        del __context
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"preparation_sha256"})
        )
        if self.preparation_sha256 != expected:
            raise ValueError("Package 8 preparation canonical hash mismatch")
        if (
            self.postprocessing_source_manifest_sha256
            != package8_postprocessing_source_sha256_v3()
        ):
            raise ValueError("Package 8 postprocessing source manifest drift")
        if self.pre_judgment_analysis.bindings != self.blinded.packet.bindings:
            raise ValueError("blinded packet does not bind the preparation analysis")
        expected_ids = tuple(
            item.observation.observation_id for item in self.provisionals
        )
        actual_ids = tuple(
            item.observation_id for item in self.blinded.unblinding_key.entries
        )
        if set(expected_ids) != set(actual_ids) or len(actual_ids) != len(
            set(actual_ids)
        ):
            raise ValueError("blinded key does not exactly cover receipt provisionals")


class Package8JudgeBindingsV3(FrozenPackage8FinalizationV3):
    """Frozen P8 source and journal identities spanning calibration and scoring."""

    schema_version: str = _PREPARATION_SCHEMA
    preparation_sha256: str = Field(pattern=_SHA256)
    postprocessing_source_manifest_sha256: str = Field(pattern=_SHA256)
    judge_configuration_sha256: str = Field(pattern=_SHA256)
    calibration_sha256: str = Field(pattern=_SHA256)
    reference_bundle_sha256: str = Field(pattern=_SHA256)
    thresholds_sha256: str = Field(pattern=_SHA256)
    development_judge_run_key_sha256: str = Field(pattern=_SHA256)
    heldout_judge_run_key_sha256: str = Field(pattern=_SHA256)
    bindings_sha256: str = Field(pattern=_SHA256)

    def model_post_init(self, __context: object) -> None:
        del __context
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"bindings_sha256"})
        )
        if self.bindings_sha256 != expected:
            raise ValueError("Package 8 judge bindings canonical hash mismatch")
        if (
            self.postprocessing_source_manifest_sha256
            != package8_postprocessing_source_sha256_v3()
        ):
            raise ValueError("Package 8 judge source manifest drift")


class Package8FinalizedResultV3(FrozenPackage8FinalizationV3):
    """All local artifacts required to publish one scored Package 8 result."""

    preparation: Package8JudgingPreparationV3
    judge_bindings: Package8JudgeBindingsV3
    judge_configuration_sha256: str = Field(pattern=_SHA256)
    calibration_sha256: str = Field(pattern=_SHA256)
    judgments: tuple[JudgmentRecordV3, ...]
    judge_accounting: JudgeAccountingV3
    analysis: EvaluationAnalysisV3
    report_sha256: str = Field(pattern=_SHA256)
    summary: Package8ResultsSummaryV3

    def model_post_init(self, __context: object) -> None:
        del __context
        if self.analysis.bindings != self.preparation.blinded.packet.bindings:
            raise ValueError("final analysis does not bind the blinded packet")
        if (
            self.judge_bindings.preparation_sha256
            != self.preparation.preparation_sha256
        ):
            raise ValueError("final result does not bind its preparation")
        if (
            self.judge_bindings.judge_configuration_sha256
            != self.judge_configuration_sha256
            or self.judge_bindings.calibration_sha256 != self.calibration_sha256
        ):
            raise ValueError("final result judge identities differ from bindings")
        if self.summary.analysis_sha256 != self.analysis.analysis_sha256:
            raise ValueError("summary does not bind final analysis")
        if self.summary.report_sha256 != self.report_sha256:
            raise ValueError("summary does not bind final report")
        if self.summary.judge_accounting != self.judge_accounting:
            raise ValueError("summary does not bind final judge accounting")
        if (
            build_evaluation_report_v3(self.analysis).report_sha256
            != self.report_sha256
        ):
            raise ValueError("final result report hash differs from its analysis")


def prepare_heldout_judging_v3(
    *,
    frozen: FrozenPackage7HeldoutInputsV3,
    schedule: Sequence[ScheduledTurnV3],
    receipts: Sequence[ObservationRunReceiptV3],
    evidence_resolver: ImmutableEvidenceResolverV3,
) -> Package8JudgingPreparationV3:
    """Create a blind packet from terminal receipts without model-judge calls.

    The pre-judgment analysis exists only to establish the immutable artifact
    bindings and accepted identity set required by the frozen blind-packet
    contract.  It is never reported as a semantic benchmark result.
    """

    turns = tuple(schedule)
    run_id = _schedule_run_id(turns)
    validate_heldout_schedule_v3(frozen, turns, run_id=run_id)
    schedule_sha256 = pilot_schedule_sha256_v3(turns)
    materialized_receipts = _materialize_receipts(receipts)
    _validate_receipt_schedule_membership(materialized_receipts, turns)
    accounting = account_observation_receipts_v3(materialized_receipts)
    completed = tuple(
        item
        for item in materialized_receipts
        if item.terminal_status.value == "completed"
    )
    expected_observation_ids = tuple(
        turn.observation_id for turn in turns if turn.observation_id is not None
    )
    if len(expected_observation_ids) != len(turns):
        raise Package8FinalizationErrorV3("heldout_schedule_observation_missing")
    admission = HeldOutReceiptAdmissionV3(
        run_id=run_id,
        protocol_sha256=frozen.protocol_sha256,
        schedule_sha256=schedule_sha256,
        expected_observation_ids=expected_observation_ids,
    )
    try:
        provisionals = convert_completed_receipts_to_provisionals_v3(
            completed,
            admission=admission,
            evidence_resolver=evidence_resolver,
        )
    except BenchmarkReportingValidationErrorV3 as exc:
        raise Package8FinalizationErrorV3("receipt_to_blind_evidence_rejected") from exc

    pre_judgment = analyze_evaluation_v3(
        frozen.protocol,
        frozen.loaded_gold.split,
        frozen.repeat_decision,
        tuple(item.observation for item in provisionals),
        run_id=run_id,
        gold_sha256=frozen.loaded_gold.gold_sha256,
        split_sha256=frozen.loaded_gold.split_sha256,
        schedule_sha256=schedule_sha256,
    )
    try:
        blinded = build_blinded_answer_packet_v3(
            frozen.protocol,
            frozen.loaded_gold.gold,
            pre_judgment,
            tuple(item.answer_evidence for item in provisionals),
            random_seed=frozen.protocol.random_seed ^ _PREPARATION_SEED_MASK,
        )
    except ValueError as exc:
        raise Package8FinalizationErrorV3("blind_packet_build_rejected") from exc

    payload = {
        "schema_version": _PREPARATION_SCHEMA,
        "run_id": run_id,
        "protocol_sha256": frozen.protocol_sha256,
        "schedule_sha256": schedule_sha256,
        "postprocessing_source_manifest_sha256": (
            package8_postprocessing_source_sha256_v3()
        ),
        "receipt_accounting": accounting.model_dump(mode="json"),
        "admission": admission.model_dump(mode="json"),
        "provisionals": [item.model_dump(mode="json") for item in provisionals],
        "pre_judgment_analysis": pre_judgment.model_dump(mode="json"),
        "blinded": blinded.model_dump(mode="json"),
    }
    return Package8JudgingPreparationV3(
        schema_version=_PREPARATION_SCHEMA,
        run_id=run_id,
        protocol_sha256=frozen.protocol_sha256,
        schedule_sha256=schedule_sha256,
        postprocessing_source_manifest_sha256=(
            package8_postprocessing_source_sha256_v3()
        ),
        receipt_accounting=accounting,
        admission=admission,
        provisionals=provisionals,
        pre_judgment_analysis=pre_judgment,
        blinded=blinded,
        preparation_sha256=canonical_sha256(payload),
    )


def build_package8_judge_bindings_v3(
    *,
    preparation: Package8JudgingPreparationV3,
    configuration: ModelJudgeConfigurationV3,
    references: CalibrationReferenceBundleV3,
    thresholds: CalibrationThresholdsV3,
    calibration: CalibrationFreezeV3,
) -> Package8JudgeBindingsV3:
    """Freeze source and run keys before either calibration or held-out scoring."""

    preparation = Package8JudgingPreparationV3.model_validate(
        preparation.model_dump(mode="json")
    )
    configuration = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    references = CalibrationReferenceBundleV3.model_validate(
        references.model_dump(mode="json")
    )
    thresholds = CalibrationThresholdsV3.model_validate(
        thresholds.model_dump(mode="json")
    )
    calibration = CalibrationFreezeV3.model_validate(
        calibration.model_dump(mode="json")
    )
    source_manifest = package8_postprocessing_source_sha256_v3()
    if preparation.postprocessing_source_manifest_sha256 != source_manifest:
        raise Package8FinalizationErrorV3("postprocessing_source_manifest_drift")
    if configuration.bindings != preparation.blinded.packet.bindings:
        raise Package8FinalizationErrorV3("judge_configuration_packet_binding_drift")
    if references.protocol_sha256 != preparation.protocol_sha256:
        raise Package8FinalizationErrorV3("calibration_reference_protocol_drift")
    if (
        calibration.protocol_sha256 != preparation.protocol_sha256
        or calibration.configuration_sha256 != configuration.configuration_sha256
        or calibration.reference_bundle_sha256 != references.reference_bundle_sha256
        or calibration.thresholds_sha256 != thresholds.thresholds_sha256
    ):
        raise Package8FinalizationErrorV3("calibration_freeze_binding_drift")
    development_key = build_development_judge_run_key_v3(
        configuration=configuration,
        references=references,
        thresholds=thresholds,
        additive_source_manifest_sha256=source_manifest,
    )
    heldout_key = build_heldout_judge_run_key_v3(
        packet=preparation.blinded.packet,
        configuration=configuration,
        calibration=calibration,
        additive_source_manifest_sha256=source_manifest,
    )
    payload = {
        "schema_version": _PREPARATION_SCHEMA,
        "preparation_sha256": preparation.preparation_sha256,
        "postprocessing_source_manifest_sha256": source_manifest,
        "judge_configuration_sha256": configuration.configuration_sha256,
        "calibration_sha256": calibration.calibration_sha256,
        "reference_bundle_sha256": references.reference_bundle_sha256,
        "thresholds_sha256": thresholds.thresholds_sha256,
        "development_judge_run_key_sha256": development_key.run_key_sha256,
        "heldout_judge_run_key_sha256": heldout_key.run_key_sha256,
    }
    return Package8JudgeBindingsV3(
        **payload,
        bindings_sha256=canonical_sha256(payload),
    )


def finalize_heldout_judging_v3(
    *,
    frozen: FrozenPackage7HeldoutInputsV3,
    preparation: Package8JudgingPreparationV3,
    judge_bindings: Package8JudgeBindingsV3,
    judge_configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
    development_judge_jobs: Sequence[JudgeJobResultV3],
    heldout_judge_jobs: Sequence[JudgeJobResultV3],
) -> Package8FinalizedResultV3:
    """Join terminal blind judgments and compute the only reportable analysis."""

    preparation = Package8JudgingPreparationV3.model_validate(
        preparation.model_dump(mode="json")
    )
    configuration = ModelJudgeConfigurationV3.model_validate(
        judge_configuration.model_dump(mode="json")
    )
    calibration = CalibrationFreezeV3.model_validate(
        calibration.model_dump(mode="json")
    )
    judge_bindings = Package8JudgeBindingsV3.model_validate(
        judge_bindings.model_dump(mode="json")
    )
    if preparation.protocol_sha256 != frozen.protocol_sha256:
        raise Package8FinalizationErrorV3("preparation_protocol_drift")
    if (
        judge_bindings.preparation_sha256 != preparation.preparation_sha256
        or judge_bindings.judge_configuration_sha256
        != configuration.configuration_sha256
        or judge_bindings.calibration_sha256 != calibration.calibration_sha256
    ):
        raise Package8FinalizationErrorV3("judge_bindings_drift")
    judge_accounting, judgments = _validate_and_account_judge_jobs(
        preparation=preparation,
        calibration=calibration,
        judge_bindings=judge_bindings,
        development_judge_jobs=development_judge_jobs,
        heldout_judge_jobs=heldout_judge_jobs,
    )
    try:
        judged = join_model_judgments_to_observations_v3(
            preparation.provisionals,
            blinded_packet=preparation.blinded.packet,
            unblinding_key=preparation.blinded.unblinding_key,
            judgments=judgments,
            judge_configuration=configuration,
            calibration=calibration,
        )
        analyzed = analyze_judged_observations_v3(
            judged,
            protocol=frozen.protocol,
            split=frozen.loaded_gold.split,
            repeat_decision=frozen.repeat_decision,
            run_id=preparation.run_id,
            gold_sha256=frozen.loaded_gold.gold_sha256,
            split_sha256=frozen.loaded_gold.split_sha256,
            schedule_sha256=preparation.schedule_sha256,
        )
        report = build_evaluation_report_v3(analyzed.analysis)
        summary = build_package8_results_summary_v3(
            accounting=preparation.receipt_accounting,
            judged=judged,
            analysis=analyzed.analysis,
            report_sha256=report.report_sha256,
            judge_accounting=judge_accounting,
        )
    except (BenchmarkReportingValidationErrorV3, ValueError) as exc:
        raise Package8FinalizationErrorV3("judged_analysis_rejected") from exc
    return Package8FinalizedResultV3(
        preparation=preparation,
        judge_bindings=judge_bindings,
        judge_configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=calibration.calibration_sha256,
        judgments=judgments,
        judge_accounting=judge_accounting,
        analysis=analyzed.analysis,
        report_sha256=report.report_sha256,
        summary=summary,
    )


def write_package8_finalization_v3(
    output_directory: Path,
    result: Package8FinalizedResultV3,
) -> None:
    """Write deterministic P8 tables/error analysis beside validated v3 artifacts."""

    result = Package8FinalizedResultV3.model_validate(result.model_dump(mode="json"))
    directory = Path(output_directory)
    publication_directory = directory / "p8-publication"
    if publication_directory.exists():
        raise FileExistsError("Package 8 final artifacts already exist")
    directory.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(tempfile.mkdtemp(prefix=".p8-publication-", dir=directory))
    artifact_directory = staging_directory / "scored-artifacts"
    report_directory = staging_directory / "p8-report"
    report = build_evaluation_report_v3(result.analysis)
    try:
        write_evaluation_artifacts_v3(
            artifact_directory,
            analysis=result.analysis,
            report=report,
            blinded=result.preparation.blinded,
            judgments=result.judgments,
        )
        report_directory.mkdir(parents=False)
        _exclusive_write(
            report_directory / "summary.json", canonical_json_bytes(result.summary)
        )
        _exclusive_write(
            report_directory / "preparation.json",
            canonical_json_bytes(result.preparation),
        )
        _exclusive_write(
            report_directory / "error-analysis.json",
            canonical_json_bytes(
                {
                    "completion_status": result.analysis.completion_status,
                    "issues": [
                        item.model_dump(mode="json") for item in result.analysis.issues
                    ],
                    "receipt_failures": [
                        item.model_dump(mode="json")
                        for item in result.preparation.receipt_accounting.failures
                    ],
                    "judge_failures": [
                        item.model_dump(mode="json")
                        for item in result.judge_accounting.failures
                    ],
                }
            ),
        )
        _write_comparison_table(report_directory / "comparisons.csv", result.analysis)
        _write_comparison_chart(report_directory / "comparisons.svg", result.analysis)
        _write_report_manifest(report_directory)
        os.replace(staging_directory, publication_directory)
    except BaseException:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise


def _materialize_receipts(
    receipts: Sequence[ObservationRunReceiptV3],
) -> tuple[ObservationRunReceiptV3, ...]:
    normalized: list[ObservationRunReceiptV3] = []
    for receipt in receipts:
        if not isinstance(receipt, ObservationRunReceiptV3):
            raise Package8FinalizationErrorV3("receipt_type_invalid")
        try:
            normalized.append(
                ObservationRunReceiptV3.model_validate(receipt.model_dump(mode="json"))
            )
        except ValueError as exc:
            raise Package8FinalizationErrorV3("receipt_validation_failed") from exc
    identifiers = [item.canonical_turn_id for item in normalized]
    if len(identifiers) != len(set(identifiers)):
        raise Package8FinalizationErrorV3("receipt_turn_id_duplicate")
    return tuple(sorted(normalized, key=lambda item: item.execution_order or -1))


def _validate_receipt_schedule_membership(
    receipts: Sequence[ObservationRunReceiptV3],
    schedule: Sequence[ScheduledTurnV3],
) -> None:
    expected = {turn.turn_id: turn for turn in schedule}
    for receipt in receipts:
        turn = expected.get(receipt.canonical_turn_id)
        if turn is None:
            raise Package8FinalizationErrorV3("receipt_turn_not_in_heldout_schedule")
        if (
            receipt.identity != turn.identity
            or receipt.run_id != turn.identity.run_id
            or receipt.protocol_sha256 != turn.identity.protocol_sha256
            or receipt.schedule_index != turn.schedule_index
            or receipt.execution_order != turn.execution_order
            or receipt.observation_id != turn.observation_id
            or receipt.schedule_sha256 != pilot_schedule_sha256_v3(tuple(schedule))
        ):
            raise Package8FinalizationErrorV3("receipt_schedule_membership_drift")


def _schedule_run_id(schedule: Sequence[ScheduledTurnV3]) -> str:
    run_ids = {item.identity.run_id for item in schedule}
    if len(run_ids) != 1:
        raise Package8FinalizationErrorV3("schedule_run_id_drift")
    return next(iter(run_ids))


def _validate_and_account_judge_jobs(
    *,
    preparation: Package8JudgingPreparationV3,
    calibration: CalibrationFreezeV3,
    judge_bindings: Package8JudgeBindingsV3,
    development_judge_jobs: Sequence[JudgeJobResultV3],
    heldout_judge_jobs: Sequence[JudgeJobResultV3],
) -> tuple[JudgeAccountingV3, tuple[JudgmentRecordV3, ...]]:
    """Require all calibrated judge jobs and retain their retry-inclusive costs."""

    development = tuple(development_judge_jobs)
    heldout = tuple(heldout_judge_jobs)
    expected_development = set(calibration.expected_calibration_ids)
    expected_heldout = {
        answer.opaque_answer_id for answer in preparation.blinded.packet.answers
    }
    _validate_judge_job_group(
        development,
        phase="development_calibration",
        expected_target_ids=expected_development,
        expected_run_key_sha256=judge_bindings.development_judge_run_key_sha256,
    )
    _validate_judge_job_group(
        heldout,
        phase="held_out_scoring",
        expected_target_ids=expected_heldout,
        expected_run_key_sha256=judge_bindings.heldout_judge_run_key_sha256,
    )
    expected_record_hashes = set(calibration.development_record_sha256s)
    actual_record_hashes: set[str] = set()
    for job in development:
        if not isinstance(job.record, CalibrationRecordV3):
            raise Package8FinalizationErrorV3("development_judge_record_type_invalid")
        if job.record.calibration_id != job.identity.target_id:
            raise Package8FinalizationErrorV3("development_judge_record_identity_drift")
        actual_record_hashes.add(job.record.record_sha256)
    if actual_record_hashes != expected_record_hashes:
        raise Package8FinalizationErrorV3("calibration_record_set_drift")
    judgments: list[JudgmentRecordV3] = []
    expected_answer_sources = {
        answer.opaque_answer_id: blinded_answer_sha256_v3(answer)
        for answer in preparation.blinded.packet.answers
    }
    for job in heldout:
        if not isinstance(job.record, JudgmentRecordV3):
            raise Package8FinalizationErrorV3("heldout_judge_record_type_invalid")
        if (
            job.identity.source_sha256
            != expected_answer_sources[job.identity.target_id]
        ):
            raise Package8FinalizationErrorV3("heldout_judge_answer_source_drift")
        judgments.append(job.record)
    return _account_judge_jobs(development, heldout), tuple(judgments)


def _validate_judge_job_group(
    jobs: Sequence[JudgeJobResultV3],
    *,
    phase: str,
    expected_target_ids: set[str],
    expected_run_key_sha256: str,
) -> None:
    if len(jobs) != len(expected_target_ids):
        raise Package8FinalizationErrorV3("judge_job_count_mismatch")
    actual_targets = {job.identity.target_id for job in jobs}
    if actual_targets != expected_target_ids:
        raise Package8FinalizationErrorV3("judge_job_target_set_mismatch")
    job_ids = {job.identity.job_id for job in jobs}
    if len(job_ids) != len(jobs):
        raise Package8FinalizationErrorV3("judge_job_identity_duplicate")
    scope_ids = {job.scope_id for job in jobs}
    if None in scope_ids or len(scope_ids) != len(jobs):
        raise Package8FinalizationErrorV3("judge_scope_identity_invalid")
    for job in jobs:
        if job.identity.phase != phase:
            raise Package8FinalizationErrorV3("judge_job_phase_mismatch")
        if job.identity.run_key_sha256 != expected_run_key_sha256:
            raise Package8FinalizationErrorV3("judge_job_run_key_drift")
        if (
            job.status != "completed"
            or job.record is None
            or job.error_code is not None
        ):
            raise Package8FinalizationErrorV3("judge_jobs_not_complete")


def _account_judge_jobs(
    development_jobs: Sequence[JudgeJobResultV3],
    heldout_jobs: Sequence[JudgeJobResultV3],
) -> JudgeAccountingV3:
    """Convert only durable journal snapshots into a non-overlapping ledger total."""

    jobs = tuple(development_jobs) + tuple(heldout_jobs)
    attempt_ids: set[str] = set()
    known_nano = 0
    unresolved_nano = 0
    input_tokens = 0
    output_tokens = 0
    provider_attempts = 0
    generation_calls = 0
    retries = 0
    for job in jobs:
        assert job.scope_id is not None
        calls: set[str] = set()
        for snapshot in job.ledger_attempts:
            attempt_id, call_id, known, unresolved, used_input, used_output = (
                _validate_judge_attempt_snapshot(snapshot, job.scope_id)
            )
            if attempt_id in attempt_ids:
                raise Package8FinalizationErrorV3("judge_attempt_identity_duplicate")
            attempt_ids.add(attempt_id)
            calls.add(call_id)
            known_nano += known
            unresolved_nano += unresolved
            input_tokens += used_input
            output_tokens += used_output
            provider_attempts += 1
        if len(calls) > 1:
            raise Package8FinalizationErrorV3("judge_scope_has_multiple_calls")
        generation_calls += len(calls)
        retries += len(job.ledger_attempts) - len(calls)
    return JudgeAccountingV3(
        development_job_count=len(development_jobs),
        heldout_job_count=len(heldout_jobs),
        completed_job_count=len(jobs),
        failed_job_count=0,
        ambiguous_job_count=0,
        known_cost_usd=Decimal(known_nano) / _NANO_USD,
        unresolved_reserved_cost_usd=Decimal(unresolved_nano) / _NANO_USD,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        generation_call_count=generation_calls,
        provider_attempt_count=provider_attempts,
        retry_count=retries,
        failures=(),
    )


def _validate_judge_attempt_snapshot(
    snapshot: Mapping[str, object], scope_id: str
) -> tuple[str, str, int, int, int, int]:
    if not isinstance(snapshot, Mapping):
        raise Package8FinalizationErrorV3("judge_attempt_snapshot_invalid")
    attempt_id = _required_snapshot_text(snapshot, "attempt_id")
    if _required_snapshot_text(snapshot, "scope_id") != scope_id:
        raise Package8FinalizationErrorV3("judge_attempt_scope_mismatch")
    call_id = _required_snapshot_text(snapshot, "call_id")
    if snapshot.get("operation") != "generation" or snapshot.get("purpose") != "judge":
        raise Package8FinalizationErrorV3("judge_attempt_operation_mismatch")
    usage_status = snapshot.get("usage_status")
    if usage_status not in {"known", "reserved", "unknown"}:
        raise Package8FinalizationErrorV3("judge_attempt_usage_status_invalid")
    reserved = _required_snapshot_nonnegative_int(snapshot, "reserved_nano_usd")
    actual = _optional_snapshot_nonnegative_int(snapshot, "actual_cost_nano_usd")
    used_input = _optional_snapshot_nonnegative_int(snapshot, "input_tokens") or 0
    used_output = _optional_snapshot_nonnegative_int(snapshot, "output_tokens") or 0
    total = _optional_snapshot_nonnegative_int(snapshot, "total_tokens")
    if total is not None and total != used_input + used_output:
        raise Package8FinalizationErrorV3("judge_attempt_token_totals_invalid")
    if usage_status == "known":
        if actual is None:
            raise Package8FinalizationErrorV3("judge_known_attempt_without_cost")
        return attempt_id, call_id, actual, 0, used_input, used_output
    return attempt_id, call_id, 0, reserved, used_input, used_output


def _required_snapshot_text(snapshot: Mapping[str, object], key: str) -> str:
    value = snapshot.get(key)
    if not isinstance(value, str) or not value:
        raise Package8FinalizationErrorV3("judge_attempt_snapshot_text_invalid")
    return value


def _required_snapshot_nonnegative_int(snapshot: Mapping[str, object], key: str) -> int:
    value = snapshot.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Package8FinalizationErrorV3("judge_attempt_snapshot_cost_invalid")
    return value


def _optional_snapshot_nonnegative_int(
    snapshot: Mapping[str, object], key: str
) -> int | None:
    value = snapshot.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Package8FinalizationErrorV3("judge_attempt_snapshot_usage_invalid")
    return value


def _exclusive_write(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_report_manifest(report_directory: Path) -> None:
    expected_paths = (
        "summary.json",
        "preparation.json",
        "error-analysis.json",
        "comparisons.csv",
        "comparisons.svg",
    )
    files: list[dict[str, object]] = []
    for name in expected_paths:
        payload = (report_directory / name).read_bytes()
        files.append(
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    manifest = {"schema_version": _PREPARATION_SCHEMA, "files": files}
    _exclusive_write(
        report_directory / "manifest.json",
        canonical_json_bytes(
            {
                **manifest,
                "manifest_sha256": canonical_sha256(manifest),
            }
        ),
    )


def _write_comparison_table(path: Path, analysis: EvaluationAnalysisV3) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            (
                "metric",
                "baseline_variant",
                "candidate_variant",
                "paired_observations",
                "mean_delta",
                "ci_lower",
                "ci_upper",
                "candidate_wins",
                "ties",
                "candidate_losses",
            )
        )
        for item in analysis.comparisons:
            interval = item.confidence_interval
            writer.writerow(
                (
                    item.metric.value,
                    item.baseline_variant_id,
                    item.candidate_variant_id,
                    item.paired_observation_count,
                    item.mean_delta,
                    None if interval is None else interval.lower,
                    None if interval is None else interval.upper,
                    item.candidate_wins,
                    item.ties,
                    item.candidate_losses,
                )
            )


def _write_comparison_chart(path: Path, analysis: EvaluationAnalysisV3) -> None:
    rows = [item for item in analysis.comparisons if item.mean_delta is not None]
    height = max(120, 32 * len(rows) + 40)
    scale = 180.0
    lines = [
        (
            '<svg xmlns="http://www.w3.org/2000/svg" width="720" '
            f'height="{height}" role="img" '
            'aria-label="Package 8 paired comparison deltas">'
        ),
        '<rect width="100%" height="100%" fill="white"/>',
        '<line x1="360" y1="20" x2="360" y2="100%" stroke="#334155"/>',
    ]
    for index, item in enumerate(rows):
        y = 36 + index * 32
        delta = float(item.mean_delta or 0)
        width = min(abs(delta) * scale, 320)
        x = 360 if delta >= 0 else 360 - width
        color = "#0f766e" if delta >= 0 else "#b91c1c"
        label = (
            f"{item.metric.value}: {item.baseline_variant_id} "
            f"→ {item.candidate_variant_id}"
        )
        lines.append(f'<text x="8" y="{y + 4}" font-size="10">{label}</text>')
        lines.append(
            (
                f'<rect x="{x:.2f}" y="{y - 8}" width="{width:.2f}" '
                f'height="14" fill="{color}"/>'
            )
        )
    lines.append("</svg>")
    _exclusive_write(path, "\n".join(lines).encode("utf-8"))
