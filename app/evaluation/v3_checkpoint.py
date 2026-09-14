"""Durable, fail-closed checkpoints for Package 7 evaluation runs.

The journal is intentionally small and boring: canonical JSON Lines, one fsync
per state transition, and a SHA-256 chain.  A ``started`` record is written
before any observation work.  If it has no terminal partner on restart, the
work is ambiguous and the caller must not execute it again automatically.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_models import ScheduledTurnV3
from app.evaluation.v3_schedule import pilot_schedule_sha256_v3

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_GENESIS_SHA256 = "0" * 64
_MAX_RECORD_BYTES = 16 * 1024 * 1024

_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: set[str] = set()


class CheckpointErrorV3(RuntimeError):
    """Base class for evaluation v3 checkpoint failures."""


class CheckpointIntegrityErrorV3(CheckpointErrorV3):
    """The journal is corrupt, truncated, foreign, or out of order."""


class CheckpointTransitionErrorV3(CheckpointErrorV3):
    """A requested checkpoint transition violates the exactly-once state machine."""


class CheckpointWriterLockedErrorV3(CheckpointErrorV3):
    """Another writer currently owns this checkpoint journal."""


class CheckpointEventV3(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class CheckpointRecordV3(BaseModel):
    """One canonical record in the append-only checkpoint journal."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["3.0"] = "3.0"
    sequence: int = Field(ge=0)
    checkpoint_id: str = Field(pattern=r"^checkpoint_[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_case_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_id: str = Field(pattern=r"^schedule_[a-f0-9]{64}$")
    schedule_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_index: int = Field(ge=0)
    turn_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    observation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    event: CheckpointEventV3
    payload: dict[str, JsonValue]
    previous_record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_event_shape(self) -> CheckpointRecordV3:
        if self.schedule_id != canonical_schedule_id_v3(self.schedule_sha256):
            raise ValueError("checkpoint schedule ID is not canonical")
        if self.event is CheckpointEventV3.STARTED and self.payload:
            raise ValueError("started checkpoint records cannot contain a payload")
        if self.event is not CheckpointEventV3.STARTED and not self.payload:
            raise ValueError("terminal checkpoint records require a payload")
        return self


@dataclass(frozen=True, slots=True)
class CheckpointStateV3:
    """Deterministic recovery state derived solely from a validated journal."""

    records: tuple[CheckpointRecordV3, ...]
    completed_turn_ids: tuple[str, ...]
    failed_turn_ids: tuple[str, ...]
    ambiguous_turn_ids: tuple[str, ...]
    orphan_started_turn_ids: tuple[str, ...]
    pending_turn_ids: tuple[str, ...]
    missing_turn_ids: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.orphan_started_turn_ids)

    @property
    def partial(self) -> bool:
        return bool(self.missing_turn_ids)

    def terminal_record(self, turn_id: str) -> CheckpointRecordV3 | None:
        return next(
            (
                record
                for record in reversed(self.records)
                if record.turn_id == turn_id
                and record.event
                in {
                    CheckpointEventV3.COMPLETED,
                    CheckpointEventV3.FAILED,
                    CheckpointEventV3.AMBIGUOUS,
                }
            ),
            None,
        )


def canonical_run_id_v3(protocol_sha256: str, run_key: str) -> str:
    """Derive a stable run ID when a caller does not already own one."""

    _validate_sha256(protocol_sha256, "protocol_sha256")
    if not isinstance(run_key, str) or not run_key.strip() or len(run_key) > 256:
        raise ValueError("run_key must be non-empty and at most 256 characters")
    digest = canonical_sha256(
        {
            "protocol_sha256": protocol_sha256,
            "run_key": run_key,
            "schema_version": "3.0",
        }
    )
    return f"run_{digest}"


def canonical_schedule_id_v3(schedule_sha256: str) -> str:
    _validate_sha256(schedule_sha256, "schedule_sha256")
    return f"schedule_{schedule_sha256}"


def load_checkpoint_v3(
    path: Path,
    *,
    run_id: str,
    protocol_sha256: str,
    execution_case_set_sha256: str,
    schedule_sha256: str,
    schedule: Sequence[ScheduledTurnV3],
) -> CheckpointStateV3:
    """Parse and validate a journal without repairing or guessing at its tail."""

    expected_schedule = _validate_expected_identity(
        run_id=run_id,
        protocol_sha256=protocol_sha256,
        execution_case_set_sha256=execution_case_set_sha256,
        schedule_sha256=schedule_sha256,
        schedule=schedule,
    )
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _state_from_records((), expected_schedule)
    except OSError as exc:
        raise CheckpointIntegrityErrorV3(
            "checkpoint journal could not be read"
        ) from exc

    if not raw:
        return _state_from_records((), expected_schedule)
    if not raw.endswith(b"\n"):
        raise CheckpointIntegrityErrorV3("checkpoint journal has a truncated tail")

    records: list[CheckpointRecordV3] = []
    previous_sha256 = _GENESIS_SHA256
    for sequence, encoded in enumerate(raw.splitlines()):
        if not encoded:
            raise CheckpointIntegrityErrorV3(
                "checkpoint journal contains a blank record"
            )
        if len(encoded) > _MAX_RECORD_BYTES:
            raise CheckpointIntegrityErrorV3("checkpoint record exceeds the size limit")
        record = _parse_record(encoded, sequence=sequence)
        if record.sequence != sequence:
            raise CheckpointIntegrityErrorV3(
                "checkpoint record sequence is reordered or duplicated"
            )
        if record.previous_record_sha256 != previous_sha256:
            raise CheckpointIntegrityErrorV3("checkpoint hash chain is broken")
        expected_record_hash = _record_sha256(record)
        if record.record_sha256 != expected_record_hash:
            raise CheckpointIntegrityErrorV3("checkpoint record hash is invalid")
        expected_checkpoint_id = _checkpoint_id(
            run_id=record.run_id,
            execution_case_set_sha256=record.execution_case_set_sha256,
            schedule_sha256=record.schedule_sha256,
            schedule_index=record.schedule_index,
            event=record.event,
        )
        if record.checkpoint_id != expected_checkpoint_id:
            raise CheckpointIntegrityErrorV3("checkpoint event ID is not canonical")
        if canonical_json_bytes(record) != encoded + b"\n":
            raise CheckpointIntegrityErrorV3("checkpoint record is not canonical JSON")
        records.append(record)
        previous_sha256 = record.record_sha256

    _validate_record_identity(
        records,
        run_id=run_id,
        protocol_sha256=protocol_sha256,
        execution_case_set_sha256=execution_case_set_sha256,
        schedule_sha256=schedule_sha256,
        schedule=expected_schedule,
    )
    return _state_from_records(tuple(records), expected_schedule)


class CheckpointWriterV3:
    """Exclusive append-only writer for one run journal."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        protocol_sha256: str,
        execution_case_set_sha256: str,
        schedule_sha256: str,
        schedule: Sequence[ScheduledTurnV3],
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.protocol_sha256 = protocol_sha256
        self.execution_case_set_sha256 = execution_case_set_sha256
        self.schedule_sha256 = schedule_sha256
        self.schedule = _validate_expected_identity(
            run_id=run_id,
            protocol_sha256=protocol_sha256,
            execution_case_set_sha256=execution_case_set_sha256,
            schedule_sha256=schedule_sha256,
            schedule=schedule,
        )
        self._journal_fd: int | None = None
        self._lock: _ExclusiveFileLock | None = None
        self._append_guard = threading.Lock()
        self._state: CheckpointStateV3 | None = None

    def __enter__(self) -> Self:
        if self._lock is not None:
            raise CheckpointTransitionErrorV3("checkpoint writer is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        resolved_journal = self.path.resolve()
        resolved_journal_path = os.path.normcase(str(resolved_journal))
        journal_lock_digest = canonical_sha256(
            {
                "journal_path": resolved_journal_path,
                "schema_version": "3.0",
            }
        )
        lock = _ExclusiveFileLock(
            resolved_journal.parent / f".evaluation-v3-{journal_lock_digest}.lock"
        )
        lock.acquire()
        self._lock = lock
        try:
            self._state = load_checkpoint_v3(
                self.path,
                run_id=self.run_id,
                protocol_sha256=self.protocol_sha256,
                execution_case_set_sha256=self.execution_case_set_sha256,
                schedule_sha256=self.schedule_sha256,
                schedule=self.schedule,
            )
            existed = self.path.exists()
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            flags |= getattr(os, "O_BINARY", 0)
            self._journal_fd = os.open(self.path, flags, 0o600)
            if not existed:
                os.fsync(self._journal_fd)
                _fsync_directory(self.path.parent)
        except BaseException:
            self._close_descriptors()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, exc, traceback
        self.close()
        return False

    @property
    def state(self) -> CheckpointStateV3:
        if self._state is None:
            raise CheckpointTransitionErrorV3("checkpoint writer is not open")
        return self._state

    def append_started(self, turn: ScheduledTurnV3) -> CheckpointRecordV3:
        with self._append_guard:
            state = self.state
            if state.orphan_started_turn_ids:
                raise CheckpointTransitionErrorV3(
                    "orphaned started work must be sealed before later execution"
                )
            if not state.pending_turn_ids or state.pending_turn_ids[0] != turn.turn_id:
                raise CheckpointTransitionErrorV3(
                    "started checkpoint is not the next pending schedule item"
                )
            return self._append(turn, CheckpointEventV3.STARTED, {})

    def append_completed(
        self,
        turn: ScheduledTurnV3,
        payload: Mapping[str, JsonValue],
    ) -> CheckpointRecordV3:
        return self._append_terminal(turn, CheckpointEventV3.COMPLETED, payload)

    def append_failed(
        self,
        turn: ScheduledTurnV3,
        payload: Mapping[str, JsonValue],
    ) -> CheckpointRecordV3:
        return self._append_terminal(turn, CheckpointEventV3.FAILED, payload)

    def append_ambiguous(
        self,
        turn: ScheduledTurnV3,
        payload: Mapping[str, JsonValue],
    ) -> CheckpointRecordV3:
        """Seal an orphan without running it or fabricating terminal evidence."""

        return self._append_terminal(turn, CheckpointEventV3.AMBIGUOUS, payload)

    def close(self) -> None:
        self._close_descriptors()

    def _append_terminal(
        self,
        turn: ScheduledTurnV3,
        event: Literal[
            CheckpointEventV3.COMPLETED,
            CheckpointEventV3.FAILED,
            CheckpointEventV3.AMBIGUOUS,
        ],
        payload: Mapping[str, JsonValue],
    ) -> CheckpointRecordV3:
        with self._append_guard:
            state = self.state
            if state.orphan_started_turn_ids != (turn.turn_id,):
                raise CheckpointTransitionErrorV3(
                    "terminal checkpoint requires this turn's unique started record"
                )
            if not payload:
                raise CheckpointTransitionErrorV3(
                    "terminal checkpoint payload cannot be empty"
                )
            return self._append(turn, event, dict(payload))

    def _append(
        self,
        turn: ScheduledTurnV3,
        event: CheckpointEventV3,
        payload: dict[str, JsonValue],
    ) -> CheckpointRecordV3:
        fd = self._journal_fd
        if fd is None:
            raise CheckpointTransitionErrorV3("checkpoint writer is not open")
        expected_turn = self.schedule[turn.schedule_index]
        if turn != expected_turn:
            raise CheckpointTransitionErrorV3(
                "checkpoint turn differs from the frozen schedule"
            )
        records = self.state.records
        sequence = len(records)
        previous_sha256 = records[-1].record_sha256 if records else _GENESIS_SHA256
        body: dict[str, JsonValue] = {
            "schema_version": "3.0",
            "sequence": sequence,
            "checkpoint_id": _checkpoint_id(
                run_id=self.run_id,
                execution_case_set_sha256=self.execution_case_set_sha256,
                schedule_sha256=self.schedule_sha256,
                schedule_index=turn.schedule_index,
                event=event,
            ),
            "run_id": self.run_id,
            "protocol_sha256": self.protocol_sha256,
            "execution_case_set_sha256": self.execution_case_set_sha256,
            "schedule_id": canonical_schedule_id_v3(self.schedule_sha256),
            "schedule_sha256": self.schedule_sha256,
            "schedule_index": turn.schedule_index,
            "turn_id": turn.turn_id,
            "observation_id": turn.observation_id,
            "event": event.value,
            "payload": payload,
            "previous_record_sha256": previous_sha256,
        }
        record = CheckpointRecordV3.model_validate(
            {**body, "record_sha256": canonical_sha256(body)}
        )
        _write_all(fd, canonical_json_bytes(record))
        os.fsync(fd)
        self._state = load_checkpoint_v3(
            self.path,
            run_id=self.run_id,
            protocol_sha256=self.protocol_sha256,
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            schedule=self.schedule,
        )
        return record

    def _close_descriptors(self) -> None:
        fd, self._journal_fd = self._journal_fd, None
        lock, self._lock = self._lock, None
        try:
            if fd is not None:
                os.close(fd)
        finally:
            try:
                if lock is not None:
                    lock.release()
            finally:
                self._state = None


def _parse_record(encoded: bytes, *, sequence: int) -> CheckpointRecordV3:
    try:
        text = encoded.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json,
        )
        return CheckpointRecordV3.model_validate(payload)
    except CheckpointIntegrityErrorV3:
        raise
    except Exception as exc:
        raise CheckpointIntegrityErrorV3(
            f"checkpoint record {sequence} is invalid"
        ) from exc


def _validate_record_identity(
    records: Sequence[CheckpointRecordV3],
    *,
    run_id: str,
    protocol_sha256: str,
    execution_case_set_sha256: str,
    schedule_sha256: str,
    schedule: tuple[ScheduledTurnV3, ...],
) -> None:
    active_index: int | None = None
    next_schedule_index = 0
    started: set[str] = set()
    terminal: set[str] = set()

    for record in records:
        if record.run_id != run_id:
            raise CheckpointIntegrityErrorV3("checkpoint belongs to a foreign run")
        if record.protocol_sha256 != protocol_sha256:
            raise CheckpointIntegrityErrorV3("checkpoint belongs to a foreign protocol")
        if record.execution_case_set_sha256 != execution_case_set_sha256:
            raise CheckpointIntegrityErrorV3(
                "checkpoint belongs to a foreign execution case set"
            )
        if record.schedule_sha256 != schedule_sha256:
            raise CheckpointIntegrityErrorV3("checkpoint belongs to a foreign schedule")
        if record.schedule_index >= len(schedule):
            raise CheckpointIntegrityErrorV3(
                "checkpoint schedule index is outside the frozen schedule"
            )
        expected = schedule[record.schedule_index]
        if (
            record.turn_id != expected.turn_id
            or record.observation_id != expected.observation_id
        ):
            raise CheckpointIntegrityErrorV3(
                "checkpoint canonical turn identity differs from the schedule"
            )

        if record.event is CheckpointEventV3.STARTED:
            if record.turn_id in started:
                raise CheckpointIntegrityErrorV3(
                    "checkpoint contains a duplicate started event"
                )
            if active_index is not None:
                raise CheckpointIntegrityErrorV3(
                    "checkpoint continued after ambiguous started work"
                )
            if record.schedule_index != next_schedule_index:
                raise CheckpointIntegrityErrorV3(
                    "checkpoint schedule events are reordered or missing"
                )
            started.add(record.turn_id)
            active_index = record.schedule_index
            continue

        if record.turn_id in terminal:
            raise CheckpointIntegrityErrorV3(
                "checkpoint contains a duplicate terminal event"
            )
        if active_index is None or record.schedule_index != active_index:
            raise CheckpointIntegrityErrorV3(
                "checkpoint terminal event has no matching started event"
            )
        terminal.add(record.turn_id)
        active_index = None
        next_schedule_index += 1


def _state_from_records(
    records: tuple[CheckpointRecordV3, ...],
    schedule: tuple[ScheduledTurnV3, ...],
) -> CheckpointStateV3:
    terminal_by_turn = {
        record.turn_id: record.event
        for record in records
        if record.event
        in {
            CheckpointEventV3.COMPLETED,
            CheckpointEventV3.FAILED,
            CheckpointEventV3.AMBIGUOUS,
        }
    }
    started_turns = {
        record.turn_id
        for record in records
        if record.event is CheckpointEventV3.STARTED
    }
    completed = tuple(
        turn.turn_id
        for turn in schedule
        if terminal_by_turn.get(turn.turn_id) is CheckpointEventV3.COMPLETED
    )
    failed = tuple(
        turn.turn_id
        for turn in schedule
        if terminal_by_turn.get(turn.turn_id) is CheckpointEventV3.FAILED
    )
    orphan_started = tuple(
        turn.turn_id
        for turn in schedule
        if turn.turn_id in started_turns and turn.turn_id not in terminal_by_turn
    )
    ambiguous = tuple(
        turn.turn_id
        for turn in schedule
        if turn.turn_id in orphan_started
        or terminal_by_turn.get(turn.turn_id) is CheckpointEventV3.AMBIGUOUS
    )
    pending = tuple(
        turn.turn_id for turn in schedule if turn.turn_id not in started_turns
    )
    completed_set = set(completed)
    missing = tuple(
        turn.turn_id for turn in schedule if turn.turn_id not in completed_set
    )
    return CheckpointStateV3(
        records=records,
        completed_turn_ids=completed,
        failed_turn_ids=failed,
        ambiguous_turn_ids=ambiguous,
        orphan_started_turn_ids=orphan_started,
        pending_turn_ids=pending,
        missing_turn_ids=missing,
    )


def _validate_expected_identity(
    *,
    run_id: str,
    protocol_sha256: str,
    execution_case_set_sha256: str,
    schedule_sha256: str,
    schedule: Sequence[ScheduledTurnV3],
) -> tuple[ScheduledTurnV3, ...]:
    if _IDENTIFIER.fullmatch(run_id) is None:
        raise ValueError("run_id is not a valid evaluation identifier")
    _validate_sha256(protocol_sha256, "protocol_sha256")
    _validate_sha256(execution_case_set_sha256, "execution_case_set_sha256")
    _validate_sha256(schedule_sha256, "schedule_sha256")
    frozen = tuple(schedule)
    if pilot_schedule_sha256_v3(frozen) != schedule_sha256:
        raise ValueError("schedule_sha256 does not match the supplied schedule")
    if [turn.schedule_index for turn in frozen] != list(range(len(frozen))):
        raise ValueError("schedule indices must be contiguous from zero")
    turn_ids = [turn.turn_id for turn in frozen]
    if len(turn_ids) != len(set(turn_ids)):
        raise ValueError("schedule turn IDs must be unique")
    observation_ids = [
        turn.observation_id for turn in frozen if turn.observation_id is not None
    ]
    if len(observation_ids) != len(set(observation_ids)):
        raise ValueError("schedule observation IDs must be unique")
    if any(turn.identity.run_id != run_id for turn in frozen):
        raise ValueError("schedule contains a foreign run ID")
    if any(turn.identity.protocol_sha256 != protocol_sha256 for turn in frozen):
        raise ValueError("schedule contains a foreign protocol hash")
    return frozen


def _record_sha256(record: CheckpointRecordV3) -> str:
    return canonical_sha256(record.model_dump(mode="json", exclude={"record_sha256"}))


def _checkpoint_id(
    *,
    run_id: str,
    execution_case_set_sha256: str,
    schedule_sha256: str,
    schedule_index: int,
    event: CheckpointEventV3,
) -> str:
    return "checkpoint_" + canonical_sha256(
        {
            "event": event.value,
            "execution_case_set_sha256": execution_case_set_sha256,
            "run_id": run_id,
            "schedule_index": schedule_index,
            "schedule_sha256": schedule_sha256,
            "schema_version": "3.0",
        }
    )


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointIntegrityErrorV3(
                f"checkpoint JSON contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_non_finite_json(value: str) -> None:
    raise CheckpointIntegrityErrorV3(
        f"checkpoint JSON contains non-finite number {value!r}"
    )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("checkpoint append made no progress")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class _ExclusiveFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self.key = os.path.normcase(str(path.resolve()))

    def acquire(self) -> None:
        with _PROCESS_LOCK_GUARD:
            if self.key in _PROCESS_LOCKS:
                raise CheckpointWriterLockedErrorV3(
                    "checkpoint journal already has a writer"
                )
            _PROCESS_LOCKS.add(self.key)
        try:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
            self.fd = os.open(self.path, flags, 0o600)
            if os.fstat(self.fd).st_size == 0:
                _write_all(self.fd, b"\0")
                os.fsync(self.fd)
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl_api = cast(Any, fcntl)
                fcntl_api.flock(self.fd, fcntl_api.LOCK_EX | fcntl_api.LOCK_NB)
        except OSError as exc:
            self._release_failed_acquisition()
            raise CheckpointWriterLockedErrorV3(
                "checkpoint journal already has a writer"
            ) from exc
        except BaseException:
            self._release_failed_acquisition()
            raise

    def release(self) -> None:
        fd, self.fd = self.fd, None
        try:
            if fd is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl_api = cast(Any, fcntl)
                        fcntl_api.flock(fd, fcntl_api.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            with _PROCESS_LOCK_GUARD:
                _PROCESS_LOCKS.discard(self.key)

    def _release_failed_acquisition(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)
        with _PROCESS_LOCK_GUARD:
            _PROCESS_LOCKS.discard(self.key)


__all__ = [
    "CheckpointErrorV3",
    "CheckpointEventV3",
    "CheckpointIntegrityErrorV3",
    "CheckpointRecordV3",
    "CheckpointStateV3",
    "CheckpointTransitionErrorV3",
    "CheckpointWriterLockedErrorV3",
    "CheckpointWriterV3",
    "canonical_run_id_v3",
    "canonical_schedule_id_v3",
    "load_checkpoint_v3",
]
