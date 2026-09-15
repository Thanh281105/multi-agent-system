"""Fail-closed CLI for the additive Package 8 held-out benchmark."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn, Sequence

from pydantic import BaseModel

from app.evaluation.benchmark_calibration import (
    Package8PilotCalibrationInputsV3,
    build_calibration_label_policy_v3,
    build_package8_pilot_calibration_inputs_v3,
)
from app.evaluation.benchmark_evidence import ImmutableBenchmarkEvidenceResolverV3
from app.evaluation.benchmark_finalization import (
    Package8FinalizedResultV3,
    Package8JudgeBindingsV3,
    Package8JudgingPreparationV3,
    build_package8_judge_bindings_v3,
    finalize_heldout_judging_v3,
    prepare_heldout_judging_v3,
    write_package8_finalization_v3,
)
from app.evaluation.benchmark_judging import (
    DurableModelJudgeRunnerV3,
    JudgeBudgetPolicyV3,
)
from app.evaluation.benchmark_reporting import (
    EvidenceBindingKeyV3,
    ResolvedExactEvidenceV3,
)
from app.evaluation.benchmark_v3 import (
    FrozenPackage7DriftError,
    FrozenPackage7HeldoutInputsV3,
    HeldoutEvaluationV3ObservationRunner,
    HeldoutExecutionPlanV3,
    build_heldout_execution_plan_v3,
    build_heldout_schedule_v3,
    load_frozen_package7_heldout_inputs_v3,
    run_heldout_benchmark_v3,
    write_or_validate_execution_plan_v3,
)
from app.evaluation.protocol import canonical_json_bytes
from app.evaluation.v3_checkpoint import (
    CheckpointErrorV3,
    canonical_run_id_v3,
    load_checkpoint_v3,
)
from app.evaluation.v3_judge import (
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationFreezeV3,
    ModelJudgeConfigurationV3,
    build_model_judge_configuration_v3,
    freeze_calibration_thresholds_v3,
)
from app.evaluation.v3_models import ScheduledTurnV3
from app.evaluation.v3_runner import (
    EvaluationCaseV3,
    EvaluationRunResultV3,
    ObservationExecutorFactoryV3,
    ObservationRunReceiptV3,
    execution_case_set_sha256_v3,
    execution_case_sha256_v3,
    execution_namespace_v3,
)
from app.evaluation.v3_schedule import build_pilot_schedule_v3, pilot_schedule_sha256_v3
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import (
    ConversationMode,
    EvidenceKind,
    EvidenceReference,
    TurnResult,
)
from app.v2.execution import DurableTurnOutcome

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_PLAN_NAME = "heldout-execution-plan.p8.json"
_SCHEDULE_NAME = "heldout-schedule.p8.json"
_CHECKPOINT_NAME = "heldout-checkpoint.v3.jsonl"
_PREPARATION_DIRECTORY_NAME = "p8-preparation"
_PREPARATION_NAME = "heldout-preparation.p8.json"
_CALIBRATION_INPUTS_NAME = "pilot-calibration-inputs.p8.json"
_PREPARATION_MANIFEST_NAME = "manifest.p8.json"
_JUDGE_CONFIGURATION_NAME = "judge-configuration.p8.json"
_CALIBRATION_FREEZE_NAME = "calibration-freeze.p8.json"
_JUDGE_BINDINGS_NAME = "judge-bindings.p8.json"
_FINALIZED_RESULT_NAME = "finalized-result.p8.json"
_DEVELOPMENT_JOURNAL_NAME = "development-judge.p8.jsonl"
_HELDOUT_JOURNAL_NAME = "heldout-judge.p8.jsonl"


class BenchmarkCLIError(RuntimeError):
    """A stable CLI error that never exposes provider configuration."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NoLocalCitationResolver:
    """The prepare command has no source authority and must not synthesize one."""

    def resolve(self, binding: EvidenceBindingKeyV3) -> ResolvedExactEvidenceV3 | None:
        del binding
        return None


@dataclass(frozen=True, slots=True)
class _ReceiptEvidenceAuthority:
    """Receipt-derived authority that never widens an evaluated namespace."""

    reference: EvidenceReference
    authorization: ResourceAuthorization


class _ReceiptExactEvidenceResolver:
    """Reopen exact knowledge evidence under the receipt's original authority."""

    def __init__(
        self,
        *,
        knowledge_service: object,
        corpus_version_id: str,
        index_manifest_id: str,
        authorities: Mapping[EvidenceBindingKeyV3, _ReceiptEvidenceAuthority],
    ) -> None:
        self._knowledge_service = knowledge_service
        self._corpus_version_id = corpus_version_id
        self._index_manifest_id = index_manifest_id
        self._authorities = dict(authorities)

    def resolve(self, binding: EvidenceBindingKeyV3) -> ResolvedExactEvidenceV3:
        authority = self._authorities.get(binding)
        if authority is None:
            raise BenchmarkCLIError(
                "exact_evidence_authority_unavailable",
                "no execution-produced immutable evidence authority exists "
                "for a citation",
            )
        if authority.reference.kind is not EvidenceKind.KNOWLEDGE:
            raise BenchmarkCLIError(
                "generic_exact_authority_unavailable",
                "catalog or review evidence requires a configured immutable "
                "exact authority",
            )
        resolver = ImmutableBenchmarkEvidenceResolverV3(
            self._knowledge_service,  # type: ignore[arg-type]
            authorization=authority.authorization,
            corpus_version_id=self._corpus_version_id,
            index_manifest_id=self._index_manifest_id,
            reference_source={binding: authority.reference},
        )
        try:
            return resolver.resolve(binding)
        except ValueError as exc:
            raise BenchmarkCLIError(
                "exact_evidence_resolution_failed",
                "immutable citation evidence could not be reopened under its authority",
            ) from exc


@dataclass(frozen=True, slots=True)
class _LiveOperatorResources:
    model_runtime: object
    ledger: object
    account_id: str
    knowledge_service: object
    corpus_version_id: str
    index_manifest_id: str


def cli(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            return _validate(arguments)
        if arguments.command == "dry-run":
            return _dry_run(arguments)
        if arguments.command == "prepare":
            return _prepare(arguments)
        if arguments.command == "operate":
            return _operate(arguments)
        if arguments.command in {"run", "resume"}:
            return _execute(arguments)
        raise AssertionError(f"unhandled command: {arguments.command}")
    except BenchmarkCLIError as exc:
        _emit({"status": "error", "error": exc.code, "message": str(exc)})
        return 2
    except FrozenPackage7DriftError as exc:
        _emit({"status": "error", "error": str(exc), "message": "frozen binding drift"})
        return 2
    except CheckpointErrorV3 as exc:
        _emit(
            {
                "status": "error",
                "error": "checkpoint_integrity_error",
                "message": str(exc),
            }
        )
        return 2
    except (OSError, RuntimeError, ValueError) as exc:
        _emit(
            {
                "status": "error",
                "error": "benchmark_validation_failed",
                "message": _safe_message(exc),
            }
        )
        return 2


def main() -> None:
    raise SystemExit(cli())


def _validate(arguments: argparse.Namespace) -> int:
    frozen = _load_frozen(arguments)
    _emit(
        {
            "status": "valid",
            "package7_protocol_sha256": frozen.protocol_sha256,
            "repeat_decision_sha256": frozen.repeat_decision_sha256,
            "selected_repeats": frozen.repeat_decision.selected_repeats,
            "heldout_cases": len(frozen.heldout_cases),
            "variants": len(frozen.protocol.variants),
        }
    )
    return 0


def _dry_run(arguments: argparse.Namespace) -> int:
    frozen = _load_frozen(arguments)
    run_id = _run_id(arguments, frozen.protocol_sha256)
    schedule = build_heldout_schedule_v3(frozen, run_id=run_id)
    plan = build_heldout_execution_plan_v3(frozen, schedule, run_id=run_id)
    _emit(
        {
            "status": "dry_run",
            "run_id": run_id,
            "package7_protocol_sha256": frozen.protocol_sha256,
            "repeat_decision_sha256": frozen.repeat_decision_sha256,
            "schedule_sha256": plan.heldout_schedule_sha256,
            "scheduled": len(schedule),
            "measurements": len(schedule),
            "warmups": 0,
            "network_calls": 0,
        }
    )
    return 0


def _prepare(arguments: argparse.Namespace) -> int:
    """Freeze local P8 judge inputs after a fully terminal held-out run.

    This command deliberately cannot create a service graph, provider runtime, or
    evidence resolver.  A receipt carrying a citation therefore stops preparation
    until an explicit immutable source authority is supplied by a later reviewed
    operator boundary; it never substitutes rubric or response text.
    """

    frozen = _load_frozen(arguments)
    paths = _paths(arguments)
    run_id = _run_id(arguments, frozen.protocol_sha256)
    schedule = build_heldout_schedule_v3(frozen, run_id=run_id)
    plan = build_heldout_execution_plan_v3(frozen, schedule, run_id=run_id)
    if not paths.checkpoint.is_file():
        raise BenchmarkCLIError(
            "checkpoint_missing",
            "prepare requires an existing complete held-out checkpoint",
        )
    _require_plan(paths.plan, plan)
    _require_schedule(paths.schedule, schedule)
    heldout = _load_complete_heldout_receipts(
        frozen=frozen,
        schedule=schedule,
        checkpoint=paths.checkpoint,
    )
    pilot_schedule, pilot_receipts = _load_complete_pilot_receipts(
        frozen=frozen,
        pilot_checkpoint=arguments.pilot_checkpoint,
        pilot_schedule_path=arguments.pilot_schedule,
    )
    _reject_unresolved_local_citations((*heldout, *pilot_receipts))
    calibration_inputs = build_package8_pilot_calibration_inputs_v3(
        protocol=frozen.protocol,
        loaded_gold=frozen.loaded_gold,
        pilot_schedule=pilot_schedule,
        pilot_receipts=pilot_receipts,
        evidence_resolver=_NoLocalCitationResolver(),
        policy=build_calibration_label_policy_v3(
            maximum_absolute_error={
                metric: arguments.calibration_max_absolute_error
                for metric in SEMANTIC_JUDGE_METRICS_V3
            }
        ),
    )
    preparation = prepare_heldout_judging_v3(
        frozen=frozen,
        schedule=schedule,
        receipts=heldout,
        evidence_resolver=_NoLocalCitationResolver(),
    )
    directory = paths.output / _PREPARATION_DIRECTORY_NAME
    _write_or_validate_preparation_artifacts(
        directory,
        preparation=preparation,
        calibration_inputs=calibration_inputs,
    )
    _emit(
        {
            "status": "prepared",
            "run_id": run_id,
            "package7_protocol_sha256": frozen.protocol_sha256,
            "schedule_sha256": preparation.schedule_sha256,
            "heldout_receipt_count": len(heldout),
            "blind_answer_count": preparation.blinded.packet.answer_count,
            "preparation_sha256": preparation.preparation_sha256,
            "pilot_calibration_inputs_sha256": calibration_inputs.inputs_sha256,
            "pilot_calibration_case_count": len(calibration_inputs.cases),
            "artifact_directory": str(directory.resolve()),
            "network_calls": 0,
        }
    )
    return 0


def _operate(arguments: argparse.Namespace) -> int:
    """Run the entire guarded post-SUT P8 lifecycle from immutable checkpoints.

    The journal runners own retry, replay, and idempotency semantics.  This
    coordinator only binds their inputs to write-once artifacts before dispatch
    and validates every persisted artifact again before the next lifecycle phase.
    """

    _require_operator_guard(arguments)
    frozen = _load_frozen(arguments)
    paths = _paths(arguments)
    run_id = _run_id(arguments, frozen.protocol_sha256)
    schedule = build_heldout_schedule_v3(frozen, run_id=run_id)
    plan = build_heldout_execution_plan_v3(frozen, schedule, run_id=run_id)
    if not paths.checkpoint.is_file():
        raise BenchmarkCLIError(
            "checkpoint_missing",
            "operate requires an existing complete held-out checkpoint",
        )
    _require_plan(paths.plan, plan)
    _require_schedule(paths.schedule, schedule)
    heldout = _load_complete_heldout_receipts(
        frozen=frozen,
        schedule=schedule,
        checkpoint=paths.checkpoint,
    )
    pilot_schedule, pilot_receipts = _load_complete_pilot_receipts(
        frozen=frozen,
        pilot_checkpoint=arguments.pilot_checkpoint,
        pilot_schedule_path=arguments.pilot_schedule,
    )
    pilot_cases = _load_pilot_cases(frozen)
    authorities = _receipt_evidence_authorities(
        batches=(
            (schedule, heldout, frozen.heldout_cases),
            (pilot_schedule, pilot_receipts, pilot_cases),
        )
    )

    resources = _build_live_operator_resources(
        project_root=frozen.project_root,
        database_url=arguments.database_url,
        expected_protocol=frozen.protocol,
    )
    evidence_resolver = _ReceiptExactEvidenceResolver(
        knowledge_service=resources.knowledge_service,
        corpus_version_id=resources.corpus_version_id,
        index_manifest_id=resources.index_manifest_id,
        authorities=authorities,
    )
    calibration_inputs = build_package8_pilot_calibration_inputs_v3(
        protocol=frozen.protocol,
        loaded_gold=frozen.loaded_gold,
        pilot_schedule=pilot_schedule,
        pilot_receipts=pilot_receipts,
        evidence_resolver=evidence_resolver,
        policy=build_calibration_label_policy_v3(
            maximum_absolute_error={
                metric: arguments.calibration_max_absolute_error
                for metric in SEMANTIC_JUDGE_METRICS_V3
            }
        ),
    )
    preparation = prepare_heldout_judging_v3(
        frozen=frozen,
        schedule=schedule,
        receipts=heldout,
        evidence_resolver=evidence_resolver,
    )
    directory = paths.output / _PREPARATION_DIRECTORY_NAME
    _write_or_validate_preparation_artifacts(
        directory,
        preparation=preparation,
        calibration_inputs=calibration_inputs,
    )
    preparation, calibration_inputs = _load_preparation_artifacts(directory)

    configuration = _build_judge_configuration(frozen, preparation)
    _write_or_validate_model_artifact(
        directory / _JUDGE_CONFIGURATION_NAME,
        configuration,
        label="judge_configuration",
    )
    configuration = _load_model_artifact(
        directory / _JUDGE_CONFIGURATION_NAME,
        ModelJudgeConfigurationV3,
        label="judge_configuration",
    )

    development_runner = DurableModelJudgeRunnerV3(
        runtime=resources.model_runtime,  # type: ignore[arg-type]
        ledger=resources.ledger,  # type: ignore[arg-type]
        account_id=resources.account_id,
        journal_path=directory / _DEVELOPMENT_JOURNAL_NAME,
        additive_source_manifest_sha256=(
            preparation.postprocessing_source_manifest_sha256
        ),
        budget_policy=JudgeBudgetPolicyV3(
            hard_limit_nano_usd=arguments.judge_budget_nano_usd
        ),
    )
    development_jobs = asyncio.run(
        development_runner.run_development_calibration_cases(
            configuration=configuration,
            cases=calibration_inputs.cases,
            references=calibration_inputs.references,
            thresholds=calibration_inputs.thresholds,
        )
    )
    records = tuple(job.record for job in development_jobs if job.status == "completed")
    if len(records) != len(development_jobs) or any(
        record is None for record in records
    ):
        raise BenchmarkCLIError(
            "development_calibration_incomplete",
            "development judge journal has non-complete terminal jobs",
        )
    calibration = freeze_calibration_thresholds_v3(
        configuration,
        records,  # type: ignore[arg-type]
        calibration_inputs.thresholds,
        calibration_inputs.references,
        expected_calibration_ids=calibration_inputs.expected_calibration_ids,
    )
    _write_or_validate_model_artifact(
        directory / _CALIBRATION_FREEZE_NAME,
        calibration,
        label="calibration_freeze",
    )
    calibration = _load_model_artifact(
        directory / _CALIBRATION_FREEZE_NAME,
        CalibrationFreezeV3,
        label="calibration_freeze",
    )
    judge_bindings = build_package8_judge_bindings_v3(
        preparation=preparation,
        configuration=configuration,
        references=calibration_inputs.references,
        thresholds=calibration_inputs.thresholds,
        calibration=calibration,
    )
    _write_or_validate_model_artifact(
        directory / _JUDGE_BINDINGS_NAME,
        judge_bindings,
        label="judge_bindings",
    )
    judge_bindings = _load_model_artifact(
        directory / _JUDGE_BINDINGS_NAME,
        Package8JudgeBindingsV3,
        label="judge_bindings",
    )

    heldout_runner = DurableModelJudgeRunnerV3(
        runtime=resources.model_runtime,  # type: ignore[arg-type]
        ledger=resources.ledger,  # type: ignore[arg-type]
        account_id=resources.account_id,
        journal_path=directory / _HELDOUT_JOURNAL_NAME,
        additive_source_manifest_sha256=(
            preparation.postprocessing_source_manifest_sha256
        ),
        budget_policy=JudgeBudgetPolicyV3(
            hard_limit_nano_usd=arguments.judge_budget_nano_usd
        ),
    )
    heldout_jobs = asyncio.run(
        heldout_runner.run_heldout_packet(
            packet=preparation.blinded.packet,
            configuration=configuration,
            calibration=calibration,
        )
    )
    finalized = finalize_heldout_judging_v3(
        frozen=frozen,
        preparation=preparation,
        judge_bindings=judge_bindings,
        judge_configuration=configuration,
        calibration=calibration,
        development_judge_jobs=development_jobs,
        heldout_judge_jobs=heldout_jobs,
    )
    _write_or_validate_model_artifact(
        paths.output / _FINALIZED_RESULT_NAME,
        finalized,
        label="finalized_result",
    )
    _write_or_validate_publication(paths.output, finalized)
    summary = finalized.summary.model_dump(mode="json")
    _emit(
        {
            "status": "complete",
            "run_id": run_id,
            "preparation_sha256": preparation.preparation_sha256,
            "calibration_sha256": calibration.calibration_sha256,
            "report_sha256": finalized.report_sha256,
            "accepted_observations": finalized.summary.accepted_observation_count,
            "expected_observations": finalized.summary.expected_observation_count,
            "total_effective_cost_usd": summary["total_effective_cost_usd"],
            "total_provider_attempt_count": summary["total_provider_attempt_count"],
            "total_retry_count": summary["total_retry_count"],
            "artifact_directory": str(directory.resolve()),
            "publication_directory": str((paths.output / "p8-publication").resolve()),
        }
    )
    return 0


def _execute(arguments: argparse.Namespace) -> int:
    frozen = _load_frozen(arguments)
    paths = _paths(arguments)
    run_id = _run_id(arguments, frozen.protocol_sha256)
    schedule = build_heldout_schedule_v3(frozen, run_id=run_id)
    plan = build_heldout_execution_plan_v3(frozen, schedule, run_id=run_id)

    if arguments.command == "run" and paths.checkpoint.exists():
        raise BenchmarkCLIError(
            "checkpoint_already_exists",
            "run refuses an existing held-out checkpoint; use resume",
        )
    if arguments.command == "resume":
        if not paths.checkpoint.is_file():
            raise BenchmarkCLIError(
                "checkpoint_missing",
                "resume requires an existing held-out checkpoint",
            )
        _require_plan(paths.plan, plan)
        _require_schedule(paths.schedule, schedule)
        runner = HeldoutEvaluationV3ObservationRunner(
            frozen=frozen,
            schedule=schedule,
            checkpoint_path=paths.checkpoint,
            executor_factory=_ForbiddenFactory(),
        )
        state = load_checkpoint_v3(
            paths.checkpoint,
            run_id=runner.run_id,
            protocol_sha256=frozen.protocol_sha256,
            execution_case_set_sha256=runner.execution_case_set_sha256,
            schedule_sha256=runner.schedule_sha256,
            schedule=schedule,
        )
        if state.orphan_started_turn_ids:
            result = runner.seal_orphan_started_observations()
            _emit_run_result(result, paths.checkpoint)
            return 3
        if not state.pending_turn_ids:
            result = asyncio.run(runner.run())
            _emit_run_result(result, paths.checkpoint)
            return 3 if result.summary.is_partial else 0

    if not arguments.allow_network:
        raise BenchmarkCLIError(
            "network_consent_required",
            "held-out execution requires explicit --allow-network consent",
        )
    if arguments.database_url is None:
        raise BenchmarkCLIError(
            "database_url_required",
            "held-out execution requires an explicit --database-url",
        )
    write_or_validate_execution_plan_v3(paths.plan, plan)
    _write_or_validate_schedule(paths.schedule, schedule)
    try:
        factory = _build_package7_live_executor_factory(
            project_root=frozen.project_root,
            database_url=arguments.database_url,
            expected_protocol=frozen.protocol,
        )
        result = asyncio.run(
            run_heldout_benchmark_v3(
                frozen=frozen,
                schedule=schedule,
                checkpoint_path=paths.checkpoint,
                executor_factory=factory,
            )
        )
    except (CheckpointErrorV3, BenchmarkCLIError, FrozenPackage7DriftError):
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _emit_partial_environment(paths.checkpoint, frozen, schedule, run_id, exc)
        return 3
    _emit_run_result(result, paths.checkpoint)
    return 3 if result.summary.is_partial else 0


def _load_complete_heldout_receipts(
    *,
    frozen: FrozenPackage7HeldoutInputsV3,
    schedule: tuple[ScheduledTurnV3, ...],
    checkpoint: Path,
) -> tuple[ObservationRunReceiptV3, ...]:
    """Read a terminal P8 journal with a factory that cannot dispatch work."""

    runner = HeldoutEvaluationV3ObservationRunner(
        frozen=frozen,
        schedule=schedule,
        checkpoint_path=checkpoint,
        executor_factory=_ForbiddenFactory(),
    )
    result = asyncio.run(runner.run())
    _require_complete_result(
        result,
        expected_count=len(schedule),
        label="heldout",
    )
    return result.receipts


def _load_complete_pilot_receipts(
    *,
    frozen: FrozenPackage7HeldoutInputsV3,
    pilot_checkpoint: Path,
    pilot_schedule_path: Path,
) -> tuple[tuple[ScheduledTurnV3, ...], tuple[ObservationRunReceiptV3, ...]]:
    """Read P7 pilot receipts only through its non-dispatch recovery path."""

    from app.evaluation import v3_cli
    from app.evaluation.v3_runner import run_observations_v3

    schedule = _read_schedule_file(pilot_schedule_path, label="pilot")
    if not schedule:
        raise BenchmarkCLIError("pilot_schedule_invalid", "pilot schedule is empty")
    pilot_run_id = schedule[0].identity.run_id
    expected = build_pilot_schedule_v3(
        frozen.protocol,
        run_id=pilot_run_id,
        protocol_sha256=frozen.protocol_sha256,
    )
    if schedule != expected:
        raise BenchmarkCLIError(
            "pilot_schedule_drift",
            "stored pilot schedule differs from the frozen Package 7 schedule",
        )
    if not pilot_checkpoint.is_file():
        raise BenchmarkCLIError(
            "pilot_checkpoint_missing",
            "prepare requires the completed Package 7 pilot checkpoint",
        )
    arguments = v3_cli._parser().parse_args(
        ("validate", "--project-root", str(frozen.project_root))
    )
    inputs = v3_cli._load_inputs(arguments)
    if inputs.protocol != frozen.protocol:
        raise FrozenPackage7DriftError("package7_pilot_protocol_binding_drift")
    result = asyncio.run(
        run_observations_v3(
            protocol=inputs.protocol,
            schedule=schedule,
            checkpoint_path=pilot_checkpoint,
            cases=inputs.cases,
            executor_factory=_ForbiddenFactory(),
        )
    )
    _require_complete_result(result, expected_count=len(schedule), label="pilot")
    return schedule, result.receipts


def _require_complete_result(
    result: EvaluationRunResultV3,
    *,
    expected_count: int,
    label: str,
) -> None:
    summary = result.summary
    if (
        summary.total_scheduled != expected_count
        or len(summary.completed_turn_ids) != expected_count
        or summary.failed_turn_ids
        or summary.ambiguous_turn_ids
        or summary.orphan_started_turn_ids
        or summary.missing_turn_ids
        or len(result.receipts) != expected_count
    ):
        raise BenchmarkCLIError(
            f"{label}_checkpoint_incomplete",
            f"prepare requires a complete, unambiguous {label} checkpoint",
        )


def _read_schedule_file(path: Path, *, label: str) -> tuple[ScheduledTurnV3, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise TypeError
        return tuple(ScheduledTurnV3.model_validate(item) for item in raw)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise BenchmarkCLIError(
            f"{label}_schedule_invalid",
            f"stored {label} schedule is missing or invalid",
        ) from exc


def _reject_unresolved_local_citations(
    receipts: Sequence[ObservationRunReceiptV3],
) -> None:
    """Prevent local preparation from fabricating citation text or authority."""

    for receipt in receipts:
        for turn in receipt.turn_results:
            try:
                durable = DurableTurnOutcome.model_validate(turn.result_payload)
            except ValueError as exc:
                raise BenchmarkCLIError(
                    "receipt_result_invalid",
                    "a completed receipt has no durable final result",
                ) from exc
            if durable.result is not None and durable.result.citations:
                raise BenchmarkCLIError(
                    "citation_authority_required",
                    "prepare requires immutable exact evidence authority "
                    "for cited results",
                )


def _write_or_validate_preparation_artifacts(
    directory: Path,
    *,
    preparation: Package8JudgingPreparationV3,
    calibration_inputs: Package8PilotCalibrationInputsV3,
) -> None:
    """Atomically persist local judge prerequisites or reject any byte drift."""

    expected = {
        _PREPARATION_NAME: canonical_json_bytes(preparation),
        _CALIBRATION_INPUTS_NAME: canonical_json_bytes(calibration_inputs),
    }
    manifest_payload = {
        "schema_version": "8.0",
        "preparation_sha256": preparation.preparation_sha256,
        "pilot_calibration_inputs_sha256": calibration_inputs.inputs_sha256,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in sorted(expected.items())
        ],
    }
    expected[_PREPARATION_MANIFEST_NAME] = canonical_json_bytes(manifest_payload)
    if directory.exists():
        _require_preparation_artifact_set(directory, expected)
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".p8-preparation-", dir=directory.parent))
    try:
        for name, payload in expected.items():
            _exclusive_write(staging / name, payload)
        os.replace(staging, directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_preparation_artifacts(
    directory: Path,
) -> tuple[Package8JudgingPreparationV3, Package8PilotCalibrationInputsV3]:
    """Reload the write-once prerequisite artifacts before judge dispatch."""

    try:
        preparation = Package8JudgingPreparationV3.model_validate_json(
            (directory / _PREPARATION_NAME).read_text(encoding="utf-8")
        )
        calibration_inputs = Package8PilotCalibrationInputsV3.model_validate_json(
            (directory / _CALIBRATION_INPUTS_NAME).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BenchmarkCLIError(
            "preparation_artifact_invalid",
            "frozen Package 8 preparation artifacts are missing or invalid",
        ) from exc
    return preparation, calibration_inputs


def _write_or_validate_model_artifact(
    path: Path,
    artifact: BaseModel,
    *,
    label: str,
) -> None:
    """Persist a canonical P8 phase artifact once and reject all byte drift."""

    expected = canonical_json_bytes(artifact)
    if path.exists():
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise BenchmarkCLIError(
                f"{label}_artifact_invalid",
                f"existing {label} artifact cannot be read",
            ) from exc
        if path.is_symlink() or actual != expected:
            raise BenchmarkCLIError(
                f"{label}_artifact_drift",
                f"existing {label} artifact differs from the frozen lifecycle input",
            )
        return
    _exclusive_write(path, expected)


def _load_model_artifact[T: BaseModel](
    path: Path,
    model: type[T],
    *,
    label: str,
) -> T:
    try:
        if path.is_symlink():
            raise OSError("artifact symlink is not allowed")
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BenchmarkCLIError(
            f"{label}_artifact_invalid",
            f"frozen {label} artifact is missing or invalid",
        ) from exc


def _write_or_validate_publication(
    output: Path,
    result: Package8FinalizedResultV3,
) -> None:
    """Publish once or verify the existing report still binds the final result."""

    publication = output / "p8-publication"
    if not publication.exists():
        write_package8_finalization_v3(output, result)
        return
    report = publication / "p8-report"
    required = {
        "summary.json": canonical_json_bytes(result.summary),
        "preparation.json": canonical_json_bytes(result.preparation),
    }
    if publication.is_symlink() or not report.is_dir() or report.is_symlink():
        raise BenchmarkCLIError(
            "publication_artifact_invalid",
            "existing Package 8 publication directory is unsafe",
        )
    for name, expected in required.items():
        path = report / name
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise BenchmarkCLIError(
                "publication_artifact_invalid",
                "existing Package 8 publication is incomplete",
            ) from exc
        if path.is_symlink() or actual != expected:
            raise BenchmarkCLIError(
                "publication_artifact_drift",
                "existing Package 8 publication differs from frozen final artifacts",
            )
    _require_report_manifest(report)


def _require_report_manifest(report: Path) -> None:
    """Verify the final report manifest instead of trusting a directory name."""

    try:
        payload = json.loads((report / "manifest.json").read_text(encoding="utf-8"))
        entries = payload["files"]
        if not isinstance(entries, list):
            raise TypeError
        expected_names = {
            "summary.json",
            "preparation.json",
            "error-analysis.json",
            "comparisons.csv",
            "comparisons.svg",
        }
        actual_names = {entry["path"] for entry in entries}
        if actual_names != expected_names or len(entries) != len(expected_names):
            raise ValueError
        for entry in entries:
            name = entry["path"]
            expected_sha = entry["sha256"]
            expected_size = entry["size_bytes"]
            if not isinstance(name, str) or not isinstance(expected_sha, str):
                raise TypeError
            if not isinstance(expected_size, int):
                raise TypeError
            path = report / name
            content = path.read_bytes()
            if path.is_symlink() or len(content) != expected_size:
                raise ValueError
            if hashlib.sha256(content).hexdigest() != expected_sha:
                raise ValueError
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BenchmarkCLIError(
            "publication_artifact_drift",
            "existing Package 8 report manifest differs from published artifacts",
        ) from exc


def _require_preparation_artifact_set(
    directory: Path,
    expected: dict[str, bytes],
) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise BenchmarkCLIError(
            "preparation_artifact_invalid",
            "existing preparation artifact directory is unsafe",
        )
    actual_names = {item.name for item in directory.iterdir()}
    if actual_names != set(expected):
        raise BenchmarkCLIError(
            "preparation_artifact_drift",
            "existing preparation artifact file set differs from the frozen set",
        )
    for name, expected_bytes in expected.items():
        path = directory / name
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise BenchmarkCLIError(
                "preparation_artifact_invalid",
                "existing preparation artifact cannot be read",
            ) from exc
        if path.is_symlink() or actual != expected_bytes:
            raise BenchmarkCLIError(
                "preparation_artifact_drift",
                "existing preparation artifact differs from frozen inputs",
            )


def _load_frozen(arguments: argparse.Namespace) -> FrozenPackage7HeldoutInputsV3:
    return load_frozen_package7_heldout_inputs_v3(
        project_root=arguments.project_root,
        protocol_path=arguments.p7_protocol,
        repeat_decision_path=arguments.p7_repeat_decision,
        gold_path=arguments.gold,
        split_path=arguments.split,
    )


def _require_operator_guard(arguments: argparse.Namespace) -> None:
    """Reject before constructing any service graph, ledger, or provider runtime."""

    if not arguments.allow_network:
        raise BenchmarkCLIError(
            "network_consent_required",
            "Package 8 operator lifecycle requires explicit --allow-network consent",
        )
    if arguments.database_url is None:
        raise BenchmarkCLIError(
            "database_url_required",
            "Package 8 operator lifecycle requires an explicit --database-url",
        )


def _load_pilot_cases(
    frozen: FrozenPackage7HeldoutInputsV3,
) -> Mapping[str, EvaluationCaseV3]:
    """Reload P7's current verified cases before using receipt authorization."""

    from app.evaluation import v3_cli

    arguments = v3_cli._parser().parse_args(
        ("validate", "--project-root", str(frozen.project_root))
    )
    inputs = v3_cli._load_inputs(arguments)
    if inputs.protocol != frozen.protocol:
        raise FrozenPackage7DriftError("package7_pilot_protocol_binding_drift")
    return inputs.cases


def _receipt_evidence_authorities(
    *,
    batches: Sequence[
        tuple[
            Sequence[ScheduledTurnV3],
            Sequence[ObservationRunReceiptV3],
            Mapping[str, EvaluationCaseV3],
        ]
    ],
) -> dict[EvidenceBindingKeyV3, _ReceiptEvidenceAuthority]:
    """Map only execution-produced cited references to exact P7 authorities."""

    authorities: dict[EvidenceBindingKeyV3, _ReceiptEvidenceAuthority] = {}
    for schedule, receipts, cases in batches:
        turns = {turn.turn_id: turn for turn in schedule}
        schedule_sha256 = pilot_schedule_sha256_v3(tuple(schedule))
        case_set_sha256 = execution_case_set_sha256_v3(schedule, cases)
        for receipt in receipts:
            turn = turns.get(receipt.canonical_turn_id)
            if turn is None:
                raise BenchmarkCLIError(
                    "receipt_authorization_binding_invalid",
                    "receipt does not belong to its immutable schedule",
                )
            case = cases.get(turn.identity.case_id)
            if (
                case is None
                or receipt.execution_case_sha256 != execution_case_sha256_v3(case)
            ):
                raise BenchmarkCLIError(
                    "receipt_authorization_binding_invalid",
                    "receipt does not bind a current Package 7 execution case",
                )
            if (
                receipt.execution_case_set_sha256 != case_set_sha256
                or receipt.schedule_sha256 != schedule_sha256
            ):
                raise BenchmarkCLIError(
                    "receipt_authorization_binding_invalid",
                    "receipt execution authority differs from the immutable schedule",
                )
            expected_namespace = execution_namespace_v3(
                run_id=receipt.run_id,
                protocol_sha256=receipt.protocol_sha256,
                execution_case_sha256=receipt.execution_case_sha256,
                execution_case_set_sha256=receipt.execution_case_set_sha256,
                schedule_sha256=receipt.schedule_sha256,
                canonical_turn_id=receipt.canonical_turn_id,
                observation_id=receipt.observation_id,
            )
            if receipt.namespace != expected_namespace:
                raise BenchmarkCLIError(
                    "receipt_authorization_binding_invalid",
                    "receipt namespace does not match its immutable execution identity",
                )
            authorization = ResourceAuthorization(
                binding=ResourceBinding(
                    tenant_id=receipt.namespace.tenant_id,
                    principal_id=receipt.namespace.principal_id,
                    mode=ConversationMode(case.principal_role),
                    store_id="demo",
                ),
                scopes=frozenset(case.scopes),
            )
            result = _receipt_final_result(receipt)
            cited_evidence_ids = {item.evidence_id for item in result.citations}
            for reference in result.evidence:
                if reference.evidence_id not in cited_evidence_ids:
                    continue
                binding = EvidenceBindingKeyV3(
                    evidence_id=reference.evidence_id,
                    source_id=reference.source_id,
                    source_version_id=reference.source_version_id,
                    chunk_id=reference.chunk_id,
                    span_id=reference.span_id,
                )
                candidate = _ReceiptEvidenceAuthority(
                    reference=reference,
                    authorization=authorization,
                )
                existing = authorities.get(binding)
                if existing is not None and existing != candidate:
                    raise BenchmarkCLIError(
                        "evidence_authorization_ambiguous",
                        "one citation binding has more than one receipt authority",
                    )
                if reference.kind in {EvidenceKind.CATALOG, EvidenceKind.REVIEW}:
                    raise BenchmarkCLIError(
                        "generic_exact_authority_unavailable",
                        "catalog or review evidence requires a configured immutable "
                        "exact authority",
                    )
                authorities[binding] = candidate
    return authorities


def _receipt_final_result(receipt: ObservationRunReceiptV3) -> TurnResult:
    try:
        durable = DurableTurnOutcome.model_validate(
            receipt.turn_results[-1].result_payload
        )
    except (IndexError, ValueError) as exc:
        raise BenchmarkCLIError(
            "receipt_result_invalid",
            "a completed receipt has no durable final result",
        ) from exc
    if durable.result is None:
        raise BenchmarkCLIError(
            "receipt_result_invalid",
            "a completed receipt has no durable final result",
        )
    return durable.result


def _build_judge_configuration(
    frozen: FrozenPackage7HeldoutInputsV3,
    preparation: Package8JudgingPreparationV3,
) -> ModelJudgeConfigurationV3:
    """Reuse P7's frozen judge model and prompt binding without operator overrides."""

    from app.evaluation.v3_cli import PACKAGE7_JUDGE_PROMPT_V3

    policy = frozen.protocol.experiment.judgment_policy
    if policy.model_binding is None:
        raise BenchmarkCLIError(
            "judge_model_binding_missing",
            "frozen Package 7 protocol omits the model-judge binding",
        )
    return build_model_judge_configuration_v3(
        bindings=preparation.blinded.packet.bindings,
        model_binding=policy.model_binding,
        judge_prompt=PACKAGE7_JUDGE_PROMPT_V3,
    )


def _build_live_operator_resources(
    *,
    project_root: Path,
    database_url: str,
    expected_protocol: object,
) -> _LiveOperatorResources:
    """Reuse P7's guarded graph and shared ledger without exposing its settings."""

    factory = _build_package7_live_executor_factory(
        project_root=project_root,
        database_url=database_url,
        expected_protocol=expected_protocol,
    )
    composer = getattr(factory, "composer", None)
    services = getattr(factory, "shared_services", None)
    model_runtime = getattr(composer, "model_runtime", None)
    if services is None or model_runtime is None:
        raise BenchmarkCLIError(
            "operator_runtime_unavailable",
            "Package 7 live runtime does not expose the required guarded services",
        )
    ledger = getattr(services, "budget_ledger", None)
    account_id = getattr(services, "budget_account_id", None)
    knowledge_service = getattr(services, "knowledge_service", None)
    snapshot = getattr(services, "knowledge_snapshot", None)
    corpus_version_id = getattr(snapshot, "corpus_version_id", None)
    index_manifest_id = getattr(snapshot, "index_manifest_id", None)
    if (
        ledger is None
        or not isinstance(account_id, str)
        or knowledge_service is None
        or not isinstance(corpus_version_id, str)
        or not isinstance(index_manifest_id, str)
    ):
        raise BenchmarkCLIError(
            "operator_runtime_unavailable",
            "Package 7 live runtime omits the shared ledger or immutable "
            "knowledge snapshot",
        )
    return _LiveOperatorResources(
        model_runtime=model_runtime,
        ledger=ledger,
        account_id=account_id,
        knowledge_service=knowledge_service,
        corpus_version_id=corpus_version_id,
        index_manifest_id=index_manifest_id,
    )


def _build_package7_live_executor_factory(
    *,
    project_root: Path,
    database_url: str,
    expected_protocol: object,
) -> ObservationExecutorFactoryV3:
    """Delegate live setup to P7's already validated factory without logging it."""

    from app.evaluation import v3_cli

    arguments = v3_cli._parser().parse_args(
        ("validate", "--project-root", str(project_root))
    )
    inputs = v3_cli._load_inputs(arguments)
    if inputs.protocol != expected_protocol:
        raise FrozenPackage7DriftError("package7_live_protocol_binding_drift")
    return v3_cli._build_live_executor_factory(
        replace(inputs, database_url=database_url)
    )


def _run_id(arguments: argparse.Namespace, protocol_sha256: str) -> str:
    run_id = arguments.run_id or canonical_run_id_v3(
        protocol_sha256,
        "package8-heldout-v1",
    )
    if _IDENTIFIER.fullmatch(run_id) is None:
        raise BenchmarkCLIError("run_id_invalid", "run ID is invalid")
    return run_id


def _write_or_validate_schedule(
    path: Path,
    schedule: tuple[ScheduledTurnV3, ...],
) -> None:
    if path.exists():
        _require_schedule(path, schedule)
        return
    _exclusive_write(
        path,
        canonical_json_bytes([turn.model_dump(mode="json") for turn in schedule]),
    )


def _require_schedule(path: Path, expected: tuple[ScheduledTurnV3, ...]) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise TypeError
        actual = tuple(ScheduledTurnV3.model_validate(item) for item in payload)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise BenchmarkCLIError(
            "schedule_invalid",
            "held-out schedule is missing or invalid",
        ) from exc
    expected_sha256 = pilot_schedule_sha256_v3(expected)
    if actual != expected or pilot_schedule_sha256_v3(actual) != expected_sha256:
        raise BenchmarkCLIError(
            "schedule_drift",
            "stored held-out schedule differs from the frozen P8 schedule",
        )


def _require_plan(path: Path, expected: HeldoutExecutionPlanV3) -> None:
    try:
        actual = HeldoutExecutionPlanV3.model_validate_json(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BenchmarkCLIError(
            "execution_plan_invalid",
            "held-out execution plan is missing or invalid",
        ) from exc
    if actual != expected:
        raise BenchmarkCLIError(
            "execution_plan_drift",
            "stored held-out execution plan differs from current frozen bindings",
        )


def _emit_partial_environment(
    checkpoint: Path,
    frozen: FrozenPackage7HeldoutInputsV3,
    schedule: tuple[ScheduledTurnV3, ...],
    run_id: str,
    exc: BaseException,
) -> None:
    completed = failed = ambiguous = 0
    missing = len(schedule)
    if checkpoint.is_file():
        try:
            runner = HeldoutEvaluationV3ObservationRunner(
                frozen=frozen,
                schedule=schedule,
                checkpoint_path=checkpoint,
                executor_factory=_ForbiddenFactory(),
            )
            state = load_checkpoint_v3(
                checkpoint,
                run_id=runner.run_id,
                protocol_sha256=frozen.protocol_sha256,
                execution_case_set_sha256=runner.execution_case_set_sha256,
                schedule_sha256=runner.schedule_sha256,
                schedule=schedule,
            )
            completed = len(state.completed_turn_ids)
            failed = len(state.failed_turn_ids)
            ambiguous = len(state.ambiguous_turn_ids)
            missing = len(state.missing_turn_ids)
        except CheckpointErrorV3:
            pass
    _emit(
        {
            "status": "partial",
            "error": _environment_error_code(exc),
            "run_id": run_id,
            "scheduled": len(schedule),
            "completed": completed,
            "failed": failed,
            "ambiguous": ambiguous,
            "missing": missing,
            "checkpoint": str(checkpoint.resolve()),
        }
    )


def _emit_run_result(result: EvaluationRunResultV3, checkpoint: Path) -> None:
    summary = result.summary
    _emit(
        {
            "status": "partial" if summary.is_partial else "complete",
            "run_id": summary.run_id,
            "package7_protocol_sha256": summary.protocol_sha256,
            "schedule_sha256": summary.schedule_sha256,
            "scheduled": summary.total_scheduled,
            "completed": len(summary.completed_turn_ids),
            "failed": len(summary.failed_turn_ids),
            "ambiguous": len(summary.ambiguous_turn_ids),
            "missing": len(summary.missing_turn_ids),
            "blocked_on_ambiguous_work": summary.blocked_on_ambiguous_work,
            "checkpoint": str(checkpoint.resolve()),
        }
    )


class _ForbiddenFactory:
    def __call__(self, **_: object) -> NoReturn:
        raise BenchmarkCLIError(
            "resume_dispatch_forbidden",
            "resume attempted to dispatch finished or ambiguous work",
        )


def _environment_error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _IDENTIFIER.fullmatch(code):
        return code
    return "environment_unavailable"


def _safe_message(exc: BaseException) -> str:
    if (
        type(exc).__module__.startswith("app.evaluation")
        or type(exc).__module__ == "builtins"
    ):
        return str(exc)[:300]
    return "held-out benchmark validation failed"


def _paths(arguments: argparse.Namespace) -> argparse.Namespace:
    output = arguments.output.resolve()
    return argparse.Namespace(
        output=output,
        plan=output / _PLAN_NAME,
        schedule=output / _SCHEDULE_NAME,
        checkpoint=arguments.checkpoint or output / _CHECKPOINT_NAME,
    )


def _exclusive_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise BenchmarkCLIError(
            "frozen_artifact_race",
            "a held-out artifact was created concurrently",
        ) from None


def _parser() -> argparse.ArgumentParser:
    project_root = _default_project_root()
    parser = argparse.ArgumentParser(
        description=(
            "Package 8 held-out operator. Local validate, dry-run, and prepare "
            "never build live services; operate is the guarded post-SUT lifecycle."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    command_help = {
        "validate": "local: validate frozen P7 and P8 bindings",
        "dry-run": "local: construct the held-out schedule without providers",
        "prepare": "local: persist only citation-free preparation prerequisites",
        "operate": (
            "guarded: exact evidence, pilot calibration, blind judge, and publication"
        ),
        "run": "guarded: start held-out SUT execution",
        "resume": "guarded: resume held-out SUT execution",
    }
    for name in ("validate", "dry-run", "prepare", "operate", "run", "resume"):
        command = subparsers.add_parser(name, help=command_help[name])
        _add_inputs(command, project_root)
        if name != "validate":
            command.add_argument("--run-id")
        if name in {"prepare", "operate", "run", "resume"}:
            command.add_argument(
                "--output",
                "--output-dir",
                dest="output",
                type=Path,
                default=project_root / "output" / "evaluation-v3" / "heldout",
            )
            command.add_argument("--checkpoint", type=Path)
        if name in {"prepare", "operate"}:
            pilot_output = project_root / "output" / "evaluation-v3" / "pilot"
            command.add_argument(
                "--pilot-checkpoint",
                type=Path,
                default=pilot_output / "pilot-checkpoint.v3.jsonl",
            )
            command.add_argument(
                "--pilot-schedule",
                type=Path,
                default=pilot_output / "pilot-schedule.v3.json",
            )
            command.add_argument(
                "--calibration-max-absolute-error",
                type=_unit_interval,
                default=0.25,
                help="frozen per-metric development judge tolerance in [0, 1]",
            )
        if name == "operate":
            command.add_argument(
                "--allow-network",
                action="store_true",
                help="explicit consent before any live service or judge dispatch",
            )
            command.add_argument(
                "--database-url",
                help="explicit durable PostgreSQL URL (never emitted)",
            )
            command.add_argument(
                "--judge-budget-nano-usd",
                type=_positive_integer,
                required=True,
                help="per-job immutable model-judge hard limit in nano-USD",
            )
        if name in {"run", "resume"}:
            command.add_argument("--allow-network", action="store_true")
            command.add_argument(
                "--database-url",
                help="explicit durable PostgreSQL URL (never emitted)",
            )
    return parser


def _unit_interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite value in [0, 1]") from exc
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be a finite value in [0, 1]")
    return parsed


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _add_inputs(parser: argparse.ArgumentParser, project_root: Path) -> None:
    p7_output = project_root / "output" / "evaluation-v3" / "pilot"
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument(
        "--p7-protocol",
        type=Path,
        default=p7_output / "protocol.v3.json",
    )
    parser.add_argument(
        "--p7-repeat-decision",
        type=Path,
        default=p7_output / "repeat-decision.v3.json",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=project_root / "evaluation" / "v3" / "gold.v3.json",
    )
    parser.add_argument(
        "--split",
        type=Path,
        default=project_root / "evaluation" / "v3" / "split.v3.json",
    )


def _default_project_root() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "evaluation" / "v3" / "gold.v3.json").is_file():
            return candidate
    return Path.cwd()


def _emit(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
