"""Durable, blinded Package 8 model-judge execution.

This module intentionally sits beside the frozen V3 judge contracts.  It does
not alter their schema, artifact, or CLI boundaries.  Its only mutable state is
an append-only local JSONL journal, which makes a provider dispatch either
durably terminal or permanently ambiguous after an interrupted start.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, cast

from pydantic import BaseModel

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerPacketV3,
    BlindedAnswerV3,
    JudgmentRecordV3,
    build_judgment_record_v3,
)
from app.evaluation.v3_gold import EvaluationSplitV3
from app.evaluation.v3_judge import (
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationFreezeV3,
    CalibrationRecordV3,
    CalibrationReferenceBundleV3,
    CalibrationReferenceLabelsV3,
    CalibrationThresholdsV3,
    DevelopmentCalibrationCaseV3,
    ModelJudgeConfigurationV3,
    ModelJudgeOutputV3,
    ModelJudgeRequestV3,
    blinded_answer_sha256_v3,
    score_deterministic_metrics_v3,
    validate_model_judge_output_v3,
)
from app.evaluation.v3_models import EvaluationMetricV3, JudgmentModeV3
from app.shared.budget import ProviderBudgetContext, provider_budget_scope
from app.shared.model_runtime import ModelRuntime

EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3 = (
    "315efff0d596f11ea63aa60729834e54fb5d6acb9bf6b7d2b9260b96b601451f"
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}$")
_JOURNAL_SCHEMA_VERSION = "1.0"

JudgePhaseV3: TypeAlias = Literal["development_calibration", "held_out_scoring"]
JudgeJobStatusV3: TypeAlias = Literal["completed", "failed", "ambiguous"]
JudgeRecordV3: TypeAlias = CalibrationRecordV3 | JudgmentRecordV3


class FrozenJudgeRunError(RuntimeError):
    """A durable-run invariant failed before another provider dispatch."""


class JudgeJournalBusyError(FrozenJudgeRunError):
    """Another process currently owns the local judge journal."""


class JudgeBudgetLedgerV3(Protocol):
    """The small SQL-ledger surface the runner needs, injectable in tests."""

    def create_scope(
        self,
        *,
        scope_id: str,
        account_id: str,
        purpose: Literal["judge"],
        hard_limit_nano_usd: int,
        max_generation_calls: int,
        max_provider_attempts: int,
        max_concurrency: int,
    ) -> None: ...

    def scope_attempt_snapshots(self, scope_id: str) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class JudgeBudgetPolicyV3:
    """Fixed budget settings for exactly one model-judge job."""

    hard_limit_nano_usd: int
    max_generation_calls: int = 1
    max_provider_attempts: int = 2
    max_concurrency: int = 1
    max_retries: int = 1
    attempt_timeout_seconds: float = 18.0

    def __post_init__(self) -> None:
        if self.hard_limit_nano_usd < 1:
            raise ValueError("judge budget limit must be positive")
        if self.max_generation_calls != 1:
            raise ValueError("judge jobs permit exactly one logical generation call")
        if self.max_provider_attempts != 2:
            raise ValueError("judge jobs permit one retry and two provider attempts")
        if self.max_concurrency != 1:
            raise ValueError("judge jobs permit one provider attempt at a time")
        if self.max_retries != 1:
            raise ValueError("judge jobs require exactly one retry")
        if self.attempt_timeout_seconds != 18.0:
            raise ValueError("judge jobs require an 18 second attempt timeout")


@dataclass(frozen=True, slots=True)
class JudgeRunKeyV3:
    """Immutable identity for one journal and its non-replayable jobs."""

    protocol_sha256: str
    configuration_sha256: str
    calibration_sha256: str | None
    blinded_packet_sha256: str | None
    development_inputs_sha256: str | None
    additive_source_manifest_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.protocol_sha256, "protocol_sha256")
        _require_sha256(self.configuration_sha256, "configuration_sha256")
        _require_optional_sha256(self.calibration_sha256, "calibration_sha256")
        _require_optional_sha256(self.blinded_packet_sha256, "blinded_packet_sha256")
        _require_optional_sha256(
            self.development_inputs_sha256, "development_inputs_sha256"
        )
        _require_sha256(
            self.additive_source_manifest_sha256,
            "additive_source_manifest_sha256",
        )
        if self.protocol_sha256 != EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3:
            raise FrozenJudgeRunError("package7_protocol_hash_drift")
        held_out = self.blinded_packet_sha256 is not None
        development = self.development_inputs_sha256 is not None
        if held_out == development:
            raise ValueError("judge run key must identify exactly one execution phase")
        if held_out != (self.calibration_sha256 is not None):
            raise ValueError("held-out judge runs require exactly one calibration hash")

    @property
    def phase(self) -> JudgePhaseV3:
        return (
            "held_out_scoring"
            if self.blinded_packet_sha256 is not None
            else "development_calibration"
        )

    def payload(self) -> dict[str, object]:
        return {
            "protocol_sha256": self.protocol_sha256,
            "configuration_sha256": self.configuration_sha256,
            "calibration_sha256": self.calibration_sha256,
            "blinded_packet_sha256": self.blinded_packet_sha256,
            "development_inputs_sha256": self.development_inputs_sha256,
            "additive_source_manifest_sha256": self.additive_source_manifest_sha256,
        }

    @property
    def run_key_sha256(self) -> str:
        return canonical_sha256(self.payload())


def build_development_judge_run_key_v3(
    *,
    configuration: ModelJudgeConfigurationV3,
    references: CalibrationReferenceBundleV3,
    thresholds: CalibrationThresholdsV3,
    additive_source_manifest_sha256: str,
) -> JudgeRunKeyV3:
    """Build a key before calibration is frozen, using its frozen inputs."""

    configuration = _normalize_configuration(configuration)
    references = CalibrationReferenceBundleV3.model_validate(
        references.model_dump(mode="json")
    )
    thresholds = CalibrationThresholdsV3.model_validate(
        thresholds.model_dump(mode="json")
    )
    if references.protocol_sha256 != configuration.bindings.protocol_sha256:
        raise FrozenJudgeRunError("calibration_reference_protocol_mismatch")
    development_inputs_sha256 = canonical_sha256(
        {
            "reference_bundle_sha256": references.reference_bundle_sha256,
            "thresholds_sha256": thresholds.thresholds_sha256,
        }
    )
    return JudgeRunKeyV3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=None,
        blinded_packet_sha256=None,
        development_inputs_sha256=development_inputs_sha256,
        additive_source_manifest_sha256=additive_source_manifest_sha256,
    )


def build_heldout_judge_run_key_v3(
    *,
    packet: BlindedAnswerPacketV3,
    configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
    additive_source_manifest_sha256: str,
) -> JudgeRunKeyV3:
    """Validate held-out provenance and build its immutable journal key."""

    packet, configuration, calibration = _validate_heldout_inputs(
        packet, configuration, calibration
    )
    return JudgeRunKeyV3(
        protocol_sha256=packet.bindings.protocol_sha256,
        configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=calibration.calibration_sha256,
        blinded_packet_sha256=packet.packet_sha256,
        development_inputs_sha256=None,
        additive_source_manifest_sha256=additive_source_manifest_sha256,
    )


@dataclass(frozen=True, slots=True)
class JudgeJobIdentityV3:
    phase: JudgePhaseV3
    target_id: str
    source_sha256: str
    run_key_sha256: str

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("judge target ID must not be empty")
        _require_sha256(self.source_sha256, "source_sha256")
        _require_sha256(self.run_key_sha256, "run_key_sha256")

    @property
    def job_id(self) -> str:
        digest = canonical_sha256(
            {
                "phase": self.phase,
                "target_id": self.target_id,
                "source_sha256": self.source_sha256,
                "run_key_sha256": self.run_key_sha256,
            }
        )
        return f"judge_{digest[:48]}"


@dataclass(frozen=True, slots=True)
class JudgeJobResultV3:
    identity: JudgeJobIdentityV3
    status: JudgeJobStatusV3
    record: JudgeRecordV3 | None
    error_code: str | None
    scope_id: str | None
    ledger_attempts: tuple[dict[str, object], ...]
    replayed: bool

    def __post_init__(self) -> None:
        if self.status == "completed" and self.record is None:
            raise ValueError("completed judge jobs require a compatible record")
        if self.status != "completed" and self.record is not None:
            raise ValueError("non-completed judge jobs cannot carry a result record")
        if self.status == "completed" and self.error_code is not None:
            raise ValueError("completed judge jobs cannot carry an error code")


class JudgeJournalV3:
    """An exclusively locked append-only journal with one terminal row per job."""

    def __init__(self, path: Path, *, run_key: JudgeRunKeyV3) -> None:
        self.path = Path(path)
        self.run_key = run_key
        self._lock_handle: Any | None = None
        self._started: dict[str, dict[str, object]] = {}
        self._terminal: dict[str, dict[str, object]] = {}

    def __enter__(self) -> JudgeJournalV3:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = _acquire_journal_lock(
            self.path.with_suffix(f"{self.path.suffix}.lock")
        )
        try:
            self._load_or_initialize()
        except BaseException:
            _release_journal_lock(self._lock_handle)
            self._lock_handle = None
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self._lock_handle is not None:
            _release_journal_lock(self._lock_handle)
            self._lock_handle = None

    def append_started(self, identity: JudgeJobIdentityV3, *, scope_id: str) -> None:
        self._require_open()
        _require_identifier(scope_id, "scope_id")
        if identity.job_id in self._started:
            raise FrozenJudgeRunError("judge_job_already_started")
        record: dict[str, object] = {
            "record_type": "started",
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "job_id": identity.job_id,
            "phase": identity.phase,
            "target_id": identity.target_id,
            "source_sha256": identity.source_sha256,
            "scope_id": scope_id,
        }
        self._append(record)
        self._started[identity.job_id] = record

    def append_terminal(
        self,
        identity: JudgeJobIdentityV3,
        *,
        status: JudgeJobStatusV3,
        record: JudgeRecordV3 | None,
        error_code: str | None,
        scope_id: str | None,
        ledger_attempts: Sequence[Mapping[str, object]],
    ) -> None:
        self._require_open()
        if identity.job_id not in self._started:
            raise FrozenJudgeRunError("judge_terminal_without_started_record")
        if identity.job_id in self._terminal:
            raise FrozenJudgeRunError("judge_job_already_terminal")
        if status not in {"completed", "failed", "ambiguous"}:
            raise ValueError("unsupported judge terminal status")
        if status == "completed" and record is None:
            raise ValueError("completed judge journal records require an artifact")
        if status != "completed" and record is not None:
            raise ValueError("failed or ambiguous jobs cannot persist artifacts")
        terminal: dict[str, object] = {
            "record_type": "terminal",
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "job_id": identity.job_id,
            "status": status,
            "scope_id": scope_id,
            "error_code": error_code,
            "ledger_attempts": [_json_value(item) for item in ledger_attempts],
            "artifact_kind": _artifact_kind(record),
            "artifact": (None if record is None else record.model_dump(mode="json")),
        }
        self._append(terminal)
        self._terminal[identity.job_id] = terminal

    def terminal_for(self, identity: JudgeJobIdentityV3) -> dict[str, object] | None:
        self._require_open()
        return self._terminal.get(identity.job_id)

    def orphaned_identities(self) -> tuple[JudgeJobIdentityV3, ...]:
        self._require_open()
        identities: list[JudgeJobIdentityV3] = []
        for job_id, started in self._started.items():
            if job_id in self._terminal:
                continue
            identities.append(
                JudgeJobIdentityV3(
                    phase=cast(JudgePhaseV3, started["phase"]),
                    target_id=cast(str, started["target_id"]),
                    source_sha256=cast(str, started["source_sha256"]),
                    run_key_sha256=self.run_key.run_key_sha256,
                )
            )
        return tuple(identities)

    def seal_orphans(
        self,
        *,
        attempt_snapshots: Callable[[str], Sequence[Mapping[str, object]]]
        | None = None,
    ) -> tuple[JudgeJobIdentityV3, ...]:
        """Turn crash-interrupted dispatch candidates into permanent ambiguity."""

        sealed: list[JudgeJobIdentityV3] = []
        for identity in self.orphaned_identities():
            started = self._started[identity.job_id]
            self.append_terminal(
                identity,
                status="ambiguous",
                record=None,
                error_code="orphaned_started_job",
                scope_id=cast(str, started["scope_id"]),
                ledger_attempts=(
                    ()
                    if attempt_snapshots is None
                    else attempt_snapshots(cast(str, started["scope_id"]))
                ),
            )
            sealed.append(identity)
        return tuple(sealed)

    def _load_or_initialize(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            header = {
                "record_type": "header",
                "schema_version": _JOURNAL_SCHEMA_VERSION,
                "run_key": self.run_key.payload(),
                "run_key_sha256": self.run_key.run_key_sha256,
            }
            self._append(header)
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise FrozenJudgeRunError("judge_journal_not_utf8") from exc
        if not lines:
            raise FrozenJudgeRunError("judge_journal_empty_after_open")
        for line_number, line in enumerate(lines, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FrozenJudgeRunError("judge_journal_invalid_json") from exc
            if not isinstance(value, dict):
                raise FrozenJudgeRunError("judge_journal_record_not_object")
            if line_number == 1:
                self._validate_header(value)
                continue
            self._read_record(value)

    def _validate_header(self, header: Mapping[str, object]) -> None:
        if header.get("record_type") != "header":
            raise FrozenJudgeRunError("judge_journal_missing_header")
        if header.get("schema_version") != _JOURNAL_SCHEMA_VERSION:
            raise FrozenJudgeRunError("judge_journal_schema_mismatch")
        if header.get("run_key") != self.run_key.payload():
            raise FrozenJudgeRunError("judge_journal_run_key_mismatch")
        if header.get("run_key_sha256") != self.run_key.run_key_sha256:
            raise FrozenJudgeRunError("judge_journal_run_key_hash_mismatch")

    def _read_record(self, record: dict[str, object]) -> None:
        if record.get("schema_version") != _JOURNAL_SCHEMA_VERSION:
            raise FrozenJudgeRunError("judge_journal_schema_mismatch")
        record_type = record.get("record_type")
        job_id = record.get("job_id")
        if not isinstance(job_id, str):
            raise FrozenJudgeRunError("judge_journal_missing_job_id")
        if record_type == "started":
            required = ("phase", "target_id", "source_sha256", "scope_id")
            if any(not isinstance(record.get(item), str) for item in required):
                raise FrozenJudgeRunError("judge_journal_invalid_started_record")
            if job_id in self._started:
                raise FrozenJudgeRunError("judge_journal_duplicate_started_record")
            phase = record["phase"]
            if phase not in {"development_calibration", "held_out_scoring"}:
                raise FrozenJudgeRunError("judge_journal_invalid_phase")
            identity = JudgeJobIdentityV3(
                phase=cast(JudgePhaseV3, phase),
                target_id=cast(str, record["target_id"]),
                source_sha256=cast(str, record["source_sha256"]),
                run_key_sha256=self.run_key.run_key_sha256,
            )
            if identity.job_id != job_id:
                raise FrozenJudgeRunError("judge_journal_job_identity_mismatch")
            self._started[job_id] = record
            return
        if record_type == "terminal":
            if job_id not in self._started or job_id in self._terminal:
                raise FrozenJudgeRunError("judge_journal_invalid_terminal_record")
            if record.get("status") not in {"completed", "failed", "ambiguous"}:
                raise FrozenJudgeRunError("judge_journal_invalid_terminal_status")
            if not isinstance(record.get("ledger_attempts"), list):
                raise FrozenJudgeRunError("judge_journal_missing_ledger_attempts")
            artifact = record.get("artifact")
            if record["status"] == "completed":
                if not isinstance(artifact, dict):
                    raise FrozenJudgeRunError(
                        "judge_journal_completed_without_artifact"
                    )
            elif artifact is not None:
                raise FrozenJudgeRunError("judge_journal_nonterminal_artifact")
            self._terminal[job_id] = record
            return
        raise FrozenJudgeRunError("judge_journal_unknown_record_type")

    def _append(self, record: Mapping[str, object]) -> None:
        self._require_open()
        encoded = canonical_json_bytes(dict(record))
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _require_open(self) -> None:
        if self._lock_handle is None:
            raise FrozenJudgeRunError("judge_journal_not_exclusively_open")


class DurableModelJudgeRunnerV3:
    """Provider runner that converts each durable job into one V3 artifact."""

    def __init__(
        self,
        *,
        runtime: ModelRuntime,
        ledger: JudgeBudgetLedgerV3,
        account_id: str,
        journal_path: Path,
        additive_source_manifest_sha256: str,
        budget_policy: JudgeBudgetPolicyV3,
        scope_id_factory: Callable[[JudgeJobIdentityV3], str] | None = None,
    ) -> None:
        _require_identifier(account_id, "account_id")
        _require_sha256(
            additive_source_manifest_sha256,
            "additive_source_manifest_sha256",
        )
        if not callable(getattr(ledger, "create_scope", None)):
            raise FrozenJudgeRunError("judge_ledger_scope_creation_unsupported")
        if not callable(getattr(ledger, "scope_attempt_snapshots", None)):
            raise FrozenJudgeRunError("judge_ledger_attempt_snapshot_unsupported")
        self._runtime = runtime
        self._ledger = ledger
        self._account_id = account_id
        self._journal_path = Path(journal_path)
        self._source_manifest_sha256 = additive_source_manifest_sha256
        self._budget_policy = budget_policy
        self._scope_id_factory = scope_id_factory or _default_scope_id

    async def run_development_calibration_cases(
        self,
        *,
        configuration: ModelJudgeConfigurationV3,
        cases: Sequence[DevelopmentCalibrationCaseV3],
        references: CalibrationReferenceBundleV3,
        thresholds: CalibrationThresholdsV3,
    ) -> tuple[JudgeJobResultV3, ...]:
        """Run development cases under the frozen calibration inputs."""

        configuration = _normalize_configuration(configuration)
        references = CalibrationReferenceBundleV3.model_validate(
            references.model_dump(mode="json")
        )
        thresholds = CalibrationThresholdsV3.model_validate(
            thresholds.model_dump(mode="json")
        )
        key = build_development_judge_run_key_v3(
            configuration=configuration,
            references=references,
            thresholds=thresholds,
            additive_source_manifest_sha256=self._source_manifest_sha256,
        )
        normalized_cases = tuple(
            DevelopmentCalibrationCaseV3.model_validate(case.model_dump(mode="json"))
            for case in cases
        )
        reference_by_id = {item.calibration_id: item for item in references.references}
        if len(reference_by_id) != len(references.references):
            raise FrozenJudgeRunError("calibration_reference_ids_not_unique")
        if {item.calibration_id for item in normalized_cases} != set(reference_by_id):
            raise FrozenJudgeRunError("calibration_case_reference_set_mismatch")
        if len({item.calibration_id for item in normalized_cases}) != len(
            normalized_cases
        ):
            raise FrozenJudgeRunError("calibration_case_ids_not_unique")

        with JudgeJournalV3(self._journal_path, run_key=key) as journal:
            journal.seal_orphans(attempt_snapshots=self._attempt_snapshots_or_empty)
            results: list[JudgeJobResultV3] = []
            for case in normalized_cases:
                result = await self._run_development_case_in_journal(
                    journal=journal,
                    key=key,
                    configuration=configuration,
                    case=case,
                    reference=reference_by_id[case.calibration_id],
                    thresholds=thresholds,
                )
                results.append(result)
            return tuple(results)

    async def run_heldout_packet(
        self,
        *,
        packet: BlindedAnswerPacketV3,
        configuration: ModelJudgeConfigurationV3,
        calibration: CalibrationFreezeV3,
    ) -> tuple[JudgeJobResultV3, ...]:
        """Score only the blinded packet; no unblinding key is accepted here."""

        packet, configuration, calibration = _validate_heldout_inputs(
            packet, configuration, calibration
        )
        key = build_heldout_judge_run_key_v3(
            packet=packet,
            configuration=configuration,
            calibration=calibration,
            additive_source_manifest_sha256=self._source_manifest_sha256,
        )
        with JudgeJournalV3(self._journal_path, run_key=key) as journal:
            journal.seal_orphans(attempt_snapshots=self._attempt_snapshots_or_empty)
            results: list[JudgeJobResultV3] = []
            for answer in packet.answers:
                results.append(
                    await self._run_heldout_answer_in_journal(
                        journal=journal,
                        key=key,
                        packet=packet,
                        configuration=configuration,
                        calibration=calibration,
                        answer=answer,
                    )
                )
            return tuple(results)

    async def run_heldout_answer(
        self,
        *,
        packet: BlindedAnswerPacketV3,
        configuration: ModelJudgeConfigurationV3,
        calibration: CalibrationFreezeV3,
        opaque_answer_id: str,
    ) -> JudgeJobResultV3:
        """Score one answer selected only by its opaque blind identifier."""

        packet, configuration, calibration = _validate_heldout_inputs(
            packet, configuration, calibration
        )
        answer = next(
            (
                item
                for item in packet.answers
                if item.opaque_answer_id == opaque_answer_id
            ),
            None,
        )
        if answer is None:
            raise FrozenJudgeRunError("opaque_answer_not_in_blinded_packet")
        key = build_heldout_judge_run_key_v3(
            packet=packet,
            configuration=configuration,
            calibration=calibration,
            additive_source_manifest_sha256=self._source_manifest_sha256,
        )
        with JudgeJournalV3(self._journal_path, run_key=key) as journal:
            journal.seal_orphans(attempt_snapshots=self._attempt_snapshots_or_empty)
            return await self._run_heldout_answer_in_journal(
                journal=journal,
                key=key,
                packet=packet,
                configuration=configuration,
                calibration=calibration,
                answer=answer,
            )

    async def _run_development_case_in_journal(
        self,
        *,
        journal: JudgeJournalV3,
        key: JudgeRunKeyV3,
        configuration: ModelJudgeConfigurationV3,
        case: DevelopmentCalibrationCaseV3,
        reference: CalibrationReferenceLabelsV3,
        thresholds: CalibrationThresholdsV3,
    ) -> JudgeJobResultV3:
        _validate_calibration_provenance(
            configuration=configuration,
            case=case,
            reference=reference,
        )
        identity = JudgeJobIdentityV3(
            phase="development_calibration",
            target_id=case.calibration_id,
            source_sha256=case.case_sha256,
            run_key_sha256=key.run_key_sha256,
        )
        terminal = journal.terminal_for(identity)
        if terminal is not None:
            return _terminal_result(
                terminal=terminal,
                identity=identity,
                expected_phase="development_calibration",
                configuration=configuration,
                case=case,
                reference=reference,
                thresholds=thresholds,
            )
        scope_id = self._start_job(journal, identity)
        try:
            output = await self._dispatch(
                identity=identity,
                configuration=configuration,
                answer=case.answer,
                phase="development_calibration",
                calibration_sha256=None,
                scope_id=scope_id,
            )
            record = _build_calibration_record(
                configuration=configuration,
                case=case,
                reference=reference,
                thresholds=thresholds,
                output=output,
            )
            attempts = self._attempt_snapshots(scope_id)
        except BaseException as exc:
            attempts = self._attempt_snapshots_or_empty(scope_id)
            error_code = _safe_error_code(exc)
            journal.append_terminal(
                identity,
                status="failed",
                record=None,
                error_code=error_code,
                scope_id=scope_id,
                ledger_attempts=attempts,
            )
            return JudgeJobResultV3(
                identity=identity,
                status="failed",
                record=None,
                error_code=error_code,
                scope_id=scope_id,
                ledger_attempts=attempts,
                replayed=False,
            )
        journal.append_terminal(
            identity,
            status="completed",
            record=record,
            error_code=None,
            scope_id=scope_id,
            ledger_attempts=attempts,
        )
        return JudgeJobResultV3(
            identity=identity,
            status="completed",
            record=record,
            error_code=None,
            scope_id=scope_id,
            ledger_attempts=attempts,
            replayed=False,
        )

    async def _run_heldout_answer_in_journal(
        self,
        *,
        journal: JudgeJournalV3,
        key: JudgeRunKeyV3,
        packet: BlindedAnswerPacketV3,
        configuration: ModelJudgeConfigurationV3,
        calibration: CalibrationFreezeV3,
        answer: BlindedAnswerV3,
    ) -> JudgeJobResultV3:
        identity = JudgeJobIdentityV3(
            phase="held_out_scoring",
            target_id=answer.opaque_answer_id,
            source_sha256=blinded_answer_sha256_v3(answer),
            run_key_sha256=key.run_key_sha256,
        )
        terminal = journal.terminal_for(identity)
        if terminal is not None:
            return _terminal_result(
                terminal=terminal,
                identity=identity,
                expected_phase="held_out_scoring",
                packet=packet,
                configuration=configuration,
                calibration=calibration,
                answer=answer,
            )
        scope_id = self._start_job(journal, identity)
        try:
            output = await self._dispatch(
                identity=identity,
                configuration=configuration,
                answer=answer,
                phase="held_out_scoring",
                calibration_sha256=calibration.calibration_sha256,
                scope_id=scope_id,
            )
            record = _build_heldout_record(
                packet=packet,
                configuration=configuration,
                calibration=calibration,
                answer=answer,
                output=output,
            )
            attempts = self._attempt_snapshots(scope_id)
        except BaseException as exc:
            attempts = self._attempt_snapshots_or_empty(scope_id)
            error_code = _safe_error_code(exc)
            journal.append_terminal(
                identity,
                status="failed",
                record=None,
                error_code=error_code,
                scope_id=scope_id,
                ledger_attempts=attempts,
            )
            return JudgeJobResultV3(
                identity=identity,
                status="failed",
                record=None,
                error_code=error_code,
                scope_id=scope_id,
                ledger_attempts=attempts,
                replayed=False,
            )
        journal.append_terminal(
            identity,
            status="completed",
            record=record,
            error_code=None,
            scope_id=scope_id,
            ledger_attempts=attempts,
        )
        return JudgeJobResultV3(
            identity=identity,
            status="completed",
            record=record,
            error_code=None,
            scope_id=scope_id,
            ledger_attempts=attempts,
            replayed=False,
        )

    def _start_job(self, journal: JudgeJournalV3, identity: JudgeJobIdentityV3) -> str:
        scope_id = self._scope_id_factory(identity)
        _require_identifier(scope_id, "scope_id")
        self._ledger.create_scope(
            scope_id=scope_id,
            account_id=self._account_id,
            purpose="judge",
            hard_limit_nano_usd=self._budget_policy.hard_limit_nano_usd,
            max_generation_calls=self._budget_policy.max_generation_calls,
            max_provider_attempts=self._budget_policy.max_provider_attempts,
            max_concurrency=self._budget_policy.max_concurrency,
        )
        journal.append_started(identity, scope_id=scope_id)
        return scope_id

    async def _dispatch(
        self,
        *,
        identity: JudgeJobIdentityV3,
        configuration: ModelJudgeConfigurationV3,
        answer: BlindedAnswerV3,
        phase: JudgePhaseV3,
        calibration_sha256: str | None,
        scope_id: str,
    ) -> ModelJudgeOutputV3:
        """Dispatch a serialized V3 request while only the blind artifact exists."""

        request = ModelJudgeRequestV3(
            phase=phase,
            judge_prompt=configuration.judge_prompt,
            output_schema_sha256=configuration.bindings.judge_schema_sha256,
            configuration_sha256=configuration.configuration_sha256,
            calibration_sha256=calibration_sha256,
            answer=answer,
        )
        context = ProviderBudgetContext(
            ledger=cast(Any, self._ledger),
            scope_id=scope_id,
            purpose="judge",
            max_retries=self._budget_policy.max_retries,
            attempt_timeout_seconds=self._budget_policy.attempt_timeout_seconds,
            call_id_factory=lambda operation: _call_id(identity, operation),
        )
        with provider_budget_scope(context):
            result = await self._runtime.generate_structured(
                stage="evaluation.judge",
                agent_id="model_judge",
                model=configuration.model_binding.model,
                instructions=configuration.judge_prompt,
                input_text=canonical_json_bytes(request).decode("utf-8"),
                schema=ModelJudgeOutputV3,
                max_output_tokens=1_200,
                reasoning_effort=configuration.model_binding.reasoning_effort,
            )
        return validate_model_judge_output_v3(getattr(result, "value", result), answer)

    def _attempt_snapshots(self, scope_id: str) -> tuple[dict[str, object], ...]:
        snapshots = self._ledger.scope_attempt_snapshots(scope_id)
        return tuple(cast(dict[str, object], _json_value(item)) for item in snapshots)

    def _attempt_snapshots_or_empty(
        self, scope_id: str
    ) -> tuple[dict[str, object], ...]:
        try:
            return self._attempt_snapshots(scope_id)
        except BaseException:
            return ()


def _normalize_configuration(
    configuration: ModelJudgeConfigurationV3,
) -> ModelJudgeConfigurationV3:
    normalized = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    if normalized.bindings.protocol_sha256 != EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3:
        raise FrozenJudgeRunError("package7_protocol_hash_drift")
    return normalized


def _validate_heldout_inputs(
    packet: BlindedAnswerPacketV3,
    configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
) -> tuple[BlindedAnswerPacketV3, ModelJudgeConfigurationV3, CalibrationFreezeV3]:
    packet = BlindedAnswerPacketV3.model_validate(packet.model_dump(mode="json"))
    configuration = _normalize_configuration(configuration)
    calibration = CalibrationFreezeV3.model_validate(
        calibration.model_dump(mode="json")
    )
    if packet.split != EvaluationSplitV3.HELD_OUT:
        raise FrozenJudgeRunError("heldout_judge_requires_blinded_heldout_packet")
    if packet.bindings != configuration.bindings:
        raise FrozenJudgeRunError("blind_packet_configuration_bindings_mismatch")
    if calibration.protocol_sha256 != packet.bindings.protocol_sha256:
        raise FrozenJudgeRunError("calibration_protocol_mismatch")
    if calibration.configuration_sha256 != configuration.configuration_sha256:
        raise FrozenJudgeRunError("calibration_configuration_mismatch")
    return packet, configuration, calibration


def _validate_calibration_provenance(
    *,
    configuration: ModelJudgeConfigurationV3,
    case: DevelopmentCalibrationCaseV3,
    reference: CalibrationReferenceLabelsV3,
) -> None:
    if case.split != EvaluationSplitV3.DEVELOPMENT:
        raise FrozenJudgeRunError("development_calibration_split_mismatch")
    if reference.calibration_id != case.calibration_id:
        raise FrozenJudgeRunError("calibration_reference_id_mismatch")
    if reference.development_case_sha256 != case.case_sha256:
        raise FrozenJudgeRunError("calibration_reference_case_mismatch")
    if reference.answer_sha256 != blinded_answer_sha256_v3(case.answer):
        raise FrozenJudgeRunError("calibration_reference_answer_mismatch")
    if configuration.bindings.protocol_sha256 != EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3:
        raise FrozenJudgeRunError("package7_protocol_hash_drift")


def _build_calibration_record(
    *,
    configuration: ModelJudgeConfigurationV3,
    case: DevelopmentCalibrationCaseV3,
    reference: CalibrationReferenceLabelsV3,
    thresholds: CalibrationThresholdsV3,
    output: ModelJudgeOutputV3,
) -> CalibrationRecordV3:
    observed = {item.metric: item.score for item in output.verdicts}
    references = dict(reference.scores)
    errors = {
        metric: abs(references[metric] - observed[metric])
        for metric in SEMANTIC_JUDGE_METRICS_V3
    }
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.DEVELOPMENT,
        "calibration_id": case.calibration_id,
        "development_case_sha256": case.case_sha256,
        "protocol_sha256": configuration.bindings.protocol_sha256,
        "configuration_sha256": configuration.configuration_sha256,
        "thresholds_sha256": thresholds.thresholds_sha256,
        "reference_sha256": reference.reference_sha256,
        "model_output_sha256": canonical_sha256(output),
        "reference_scores": references,
        "observed_scores": observed,
        "absolute_errors": errors,
    }
    return CalibrationRecordV3(
        calibration_id=case.calibration_id,
        development_case_sha256=case.case_sha256,
        protocol_sha256=configuration.bindings.protocol_sha256,
        configuration_sha256=configuration.configuration_sha256,
        thresholds_sha256=thresholds.thresholds_sha256,
        reference_sha256=reference.reference_sha256,
        model_output_sha256=canonical_sha256(output),
        reference_scores=references,
        observed_scores=observed,
        absolute_errors=errors,
        record_sha256=canonical_sha256(payload),
    )


def _build_heldout_record(
    *,
    packet: BlindedAnswerPacketV3,
    configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
    answer: BlindedAnswerV3,
    output: ModelJudgeOutputV3,
) -> JudgmentRecordV3:
    model_scores = {item.metric: item.score for item in output.verdicts}
    deterministic_scores = score_deterministic_metrics_v3(answer)
    scores = {**model_scores, **deterministic_scores}
    score_sources: dict[
        EvaluationMetricV3,
        Literal["deterministic", "model_judge", "human_review"],
    ] = {
        **{metric: "model_judge" for metric in model_scores},
        **{metric: "deterministic" for metric in deterministic_scores},
    }
    return build_judgment_record_v3(
        bindings=packet.bindings,
        blinded_packet_sha256=packet.packet_sha256,
        opaque_answer_id=answer.opaque_answer_id,
        judgment_mode=JudgmentModeV3.MODEL_JUDGE,
        model_binding=configuration.model_binding,
        scores=scores,
        score_sources=score_sources,
        judge_configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=calibration.calibration_sha256,
        judge_output_sha256=canonical_sha256(output),
    )


def _terminal_result(
    *,
    terminal: Mapping[str, object],
    identity: JudgeJobIdentityV3,
    expected_phase: JudgePhaseV3,
    packet: BlindedAnswerPacketV3 | None = None,
    configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3 | None = None,
    answer: BlindedAnswerV3 | None = None,
    case: DevelopmentCalibrationCaseV3 | None = None,
    reference: CalibrationReferenceLabelsV3 | None = None,
    thresholds: CalibrationThresholdsV3 | None = None,
) -> JudgeJobResultV3:
    status = terminal.get("status")
    if status not in {"completed", "failed", "ambiguous"}:
        raise FrozenJudgeRunError("judge_journal_invalid_terminal_status")
    scope_id = terminal.get("scope_id")
    if scope_id is not None and not isinstance(scope_id, str):
        raise FrozenJudgeRunError("judge_journal_invalid_terminal_scope")
    attempts_raw = terminal.get("ledger_attempts")
    if not isinstance(attempts_raw, list) or any(
        not isinstance(item, dict) for item in attempts_raw
    ):
        raise FrozenJudgeRunError("judge_journal_invalid_attempt_snapshots")
    record: JudgeRecordV3 | None = None
    if status == "completed":
        artifact = terminal.get("artifact")
        if not isinstance(artifact, dict):
            raise FrozenJudgeRunError("judge_journal_completed_without_artifact")
        if expected_phase == "held_out_scoring":
            if packet is None or calibration is None or answer is None:
                raise FrozenJudgeRunError("heldout_replay_context_missing")
            replayed = JudgmentRecordV3.model_validate(artifact)
            if (
                replayed.bindings != packet.bindings
                or replayed.blinded_packet_sha256 != packet.packet_sha256
                or replayed.opaque_answer_id != answer.opaque_answer_id
                or replayed.judgment_mode != JudgmentModeV3.MODEL_JUDGE
                or replayed.model_binding != configuration.model_binding
                or replayed.judge_configuration_sha256
                != configuration.configuration_sha256
                or replayed.calibration_sha256 != calibration.calibration_sha256
            ):
                raise FrozenJudgeRunError(
                    "heldout_journal_artifact_provenance_mismatch"
                )
            record = replayed
        else:
            if case is None or reference is None or thresholds is None:
                raise FrozenJudgeRunError("calibration_replay_context_missing")
            replayed_calibration = CalibrationRecordV3.model_validate(artifact)
            if (
                replayed_calibration.calibration_id != case.calibration_id
                or replayed_calibration.development_case_sha256 != case.case_sha256
                or replayed_calibration.protocol_sha256
                != configuration.bindings.protocol_sha256
                or replayed_calibration.configuration_sha256
                != configuration.configuration_sha256
                or replayed_calibration.thresholds_sha256
                != thresholds.thresholds_sha256
                or replayed_calibration.reference_sha256 != reference.reference_sha256
            ):
                raise FrozenJudgeRunError(
                    "calibration_journal_artifact_provenance_mismatch"
                )
            record = replayed_calibration
    error_code = terminal.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        raise FrozenJudgeRunError("judge_journal_invalid_error_code")
    return JudgeJobResultV3(
        identity=identity,
        status=cast(JudgeJobStatusV3, status),
        record=record,
        error_code=error_code,
        scope_id=scope_id,
        ledger_attempts=tuple(cast(dict[str, object], item) for item in attempts_raw),
        replayed=True,
    )


def _default_scope_id(identity: JudgeJobIdentityV3) -> str:
    return f"judge_{identity.run_key_sha256[:20]}_{identity.job_id[-32:]}"


def _call_id(identity: JudgeJobIdentityV3, operation: str) -> str:
    if operation != "generation":
        raise FrozenJudgeRunError("judge_runtime_requested_unsupported_operation")
    digest = canonical_sha256({"job_id": identity.job_id, "operation": operation})
    return f"mcall_{digest[:32]}"


def _artifact_kind(record: JudgeRecordV3 | None) -> str | None:
    if record is None:
        return None
    if isinstance(record, CalibrationRecordV3):
        return "calibration_record_v3"
    return "judgment_record_v3"


def _safe_error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_.-]{1,80}", code):
        return code
    if isinstance(exc, ValueError):
        return "judge_output_contract_rejected"
    if isinstance(exc, FrozenJudgeRunError):
        return "judge_runner_invariant_error"
    return "judge_runtime_error"


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise FrozenJudgeRunError("judge_ledger_snapshot_not_json_serializable")


def _require_sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _require_optional_sha256(value: str | None, field: str) -> None:
    if value is not None:
        _require_sha256(value, field)


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} is not a supported ledger identifier")


@contextmanager
def _journal_lock(path: Path):
    handle = _acquire_journal_lock(path)
    try:
        yield handle
    finally:
        _release_journal_lock(handle)


def _acquire_journal_lock(path: Path) -> Any:
    """Use an OS-managed lock so a crashed process releases the lock safely."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0)
    if handle.read(1) == b"":
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl_module: Any = fcntl
            fcntl_module.flock(
                handle.fileno(), fcntl_module.LOCK_EX | fcntl_module.LOCK_NB
            )
    except OSError as exc:
        handle.close()
        raise JudgeJournalBusyError("judge_journal_is_exclusively_locked") from exc
    return handle


def _release_journal_lock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl_module: Any = fcntl
            fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)
    finally:
        handle.close()
