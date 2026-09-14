"""Durability and fail-closed recovery tests for evaluation v3 checkpoints."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_checkpoint import (
    CheckpointEventV3,
    CheckpointIntegrityErrorV3,
    CheckpointWriterLockedErrorV3,
    CheckpointWriterV3,
    canonical_schedule_id_v3,
    load_checkpoint_v3,
)
from app.evaluation.v3_models import (
    ObservationIdentityV3,
    ScheduledTurnKindV3,
    ScheduledTurnV3,
    canonical_observation_id_v3,
    canonical_turn_id_v3,
)
from app.evaluation.v3_schedule import (
    SCHEDULE_ALGORITHM_ID_V3,
    pilot_schedule_sha256_v3,
)

PROTOCOL_SHA = "a" * 64
EXECUTION_CASE_SET_SHA = "d" * 64
RUN_ID = "run_checkpoint_v3"


def _schedule(
    *,
    run_id: str = RUN_ID,
    protocol_sha256: str = PROTOCOL_SHA,
) -> tuple[ScheduledTurnV3, ...]:
    turns: list[ScheduledTurnV3] = []
    specifications = (
        (ScheduledTurnKindV3.WARMUP, "sa_shared_tools_rag", "case_alpha", 0),
        (ScheduledTurnKindV3.MEASURED, "ma_fixed_rag", "case_alpha", 0),
        (ScheduledTurnKindV3.MEASURED, "ma_adaptive_rag", "case_beta", 0),
    )
    execution_order = 0
    for index, (kind, variant_id, case_id, repetition) in enumerate(specifications):
        identity = ObservationIdentityV3(
            run_id=run_id,
            protocol_sha256=protocol_sha256,
            schedule_algorithm_id=SCHEDULE_ALGORITHM_ID_V3,
            turn_kind=kind,
            variant_id=variant_id,
            case_id=case_id,
            work_group_id=f"work_{case_id}",
            repetition=repetition,
        )
        measured = kind is ScheduledTurnKindV3.MEASURED
        turns.append(
            ScheduledTurnV3(
                turn_id=canonical_turn_id_v3(identity),
                observation_id=(
                    canonical_observation_id_v3(identity) if measured else None
                ),
                identity=identity,
                schedule_index=index,
                execution_order=execution_order if measured else None,
            )
        )
        if measured:
            execution_order += 1
    return tuple(turns)


def _writer(
    path: Path,
    schedule: tuple[ScheduledTurnV3, ...],
    *,
    execution_case_set_sha256: str = EXECUTION_CASE_SET_SHA,
) -> CheckpointWriterV3:
    return CheckpointWriterV3(
        path,
        run_id=schedule[0].identity.run_id,
        protocol_sha256=schedule[0].identity.protocol_sha256,
        execution_case_set_sha256=execution_case_set_sha256,
        schedule_sha256=pilot_schedule_sha256_v3(schedule),
        schedule=schedule,
    )


def _load(path: Path, schedule: tuple[ScheduledTurnV3, ...]):
    return load_checkpoint_v3(
        path,
        run_id=schedule[0].identity.run_id,
        protocol_sha256=schedule[0].identity.protocol_sha256,
        execution_case_set_sha256=EXECUTION_CASE_SET_SHA,
        schedule_sha256=pilot_schedule_sha256_v3(schedule),
        schedule=schedule,
    )


def _attempt_different_identity_writer(path: str, result_queue: Any) -> None:
    schedule = _schedule(
        run_id="run_checkpoint_v3_other",
        protocol_sha256="b" * 64,
    )
    try:
        with _writer(
            Path(path),
            schedule,
            execution_case_set_sha256="e" * 64,
        ):
            result_queue.put("acquired")
    except CheckpointWriterLockedErrorV3:
        result_queue.put("locked")
    except BaseException as exc:
        result_queue.put(f"error:{type(exc).__name__}:{exc}")


def test_terminal_records_are_durable_and_resume_skips_completed_and_failed(
    tmp_path: Path,
) -> None:
    schedule = _schedule()
    path = tmp_path / "checkpoint.jsonl"
    with _writer(path, schedule) as writer:
        writer.append_started(schedule[0])
        writer.append_completed(schedule[0], {"receipt": {"kind": "warmup"}})
        writer.append_started(schedule[1])
        writer.append_failed(schedule[1], {"receipt": {"code": "bounded_failure"}})

    state = _load(path, schedule)
    assert state.completed_turn_ids == (schedule[0].turn_id,)
    assert state.failed_turn_ids == (schedule[1].turn_id,)
    assert state.pending_turn_ids == (schedule[2].turn_id,)
    assert state.ambiguous_turn_ids == ()
    assert state.missing_turn_ids == (schedule[1].turn_id, schedule[2].turn_id)
    assert [record.sequence for record in state.records] == [0, 1, 2, 3]
    assert all(
        record.schedule_id
        == canonical_schedule_id_v3(pilot_schedule_sha256_v3(schedule))
        for record in state.records
    )
    assert all(
        record.execution_case_set_sha256 == EXECUTION_CASE_SET_SHA
        for record in state.records
    )


def test_orphan_started_record_requires_ambiguous_seal_before_following_work(
    tmp_path: Path,
) -> None:
    schedule = _schedule()
    path = tmp_path / "checkpoint.jsonl"
    with _writer(path, schedule) as writer:
        writer.append_started(schedule[0])

    state = _load(path, schedule)
    assert state.blocked is True
    assert state.ambiguous_turn_ids == (schedule[0].turn_id,)
    assert state.pending_turn_ids == (schedule[1].turn_id, schedule[2].turn_id)
    assert state.missing_turn_ids == tuple(turn.turn_id for turn in schedule)

    with _writer(path, schedule) as writer:
        writer.append_ambiguous(
            schedule[0],
            {"ambiguity": {"safe_reason": "orphan_started_no_replay"}},
        )
        writer.append_started(schedule[1])
        writer.append_completed(schedule[1], {"receipt": {"ok": True}})

    recovered = _load(path, schedule)
    assert recovered.blocked is False
    assert recovered.orphan_started_turn_ids == ()
    assert recovered.ambiguous_turn_ids == (schedule[0].turn_id,)
    assert recovered.completed_turn_ids == (schedule[1].turn_id,)
    assert recovered.pending_turn_ids == (schedule[2].turn_id,)


def test_writer_lock_excludes_a_second_writer(tmp_path: Path) -> None:
    schedule = _schedule()
    path = tmp_path / "checkpoint.jsonl"
    with _writer(path, schedule):
        with pytest.raises(CheckpointWriterLockedErrorV3):
            with _writer(path, schedule):
                pytest.fail("a second checkpoint writer acquired the same lock")

    with _writer(path, schedule):
        pass


def test_writer_lock_excludes_different_run_and_protocol_in_another_process(
    tmp_path: Path,
) -> None:
    schedule = _schedule()
    path = tmp_path / "shared-checkpoint.jsonl"
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_attempt_different_identity_writer,
        args=(str(path), result_queue),
    )

    with _writer(path, schedule):
        process.start()
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("cross-process checkpoint lock attempt did not finish")

    assert process.exitcode == 0
    assert result_queue.get(timeout=2) == "locked"
    result_queue.close()
    result_queue.join_thread()


def test_parser_rejects_bad_hash_and_truncated_json(tmp_path: Path) -> None:
    schedule = _schedule()
    original = tmp_path / "original.jsonl"
    with _writer(original, schedule) as writer:
        writer.append_started(schedule[0])
        writer.append_completed(schedule[0], {"receipt": {"ok": True}})

    bad_hash = tmp_path / "bad-hash.jsonl"
    lines = original.read_bytes().splitlines()
    document = json.loads(lines[0])
    document["record_sha256"] = "f" * 64
    bad_hash.write_bytes(canonical_json_bytes(document))
    with pytest.raises(CheckpointIntegrityErrorV3, match="hash"):
        _load(bad_hash, schedule)

    truncated = tmp_path / "truncated.jsonl"
    truncated.write_bytes(original.read_bytes()[:-1])
    with pytest.raises(CheckpointIntegrityErrorV3, match="truncated"):
        _load(truncated, schedule)


def test_parser_rejects_foreign_protocol_schedule_and_run(tmp_path: Path) -> None:
    schedule = _schedule()
    source = tmp_path / "source.jsonl"
    with _writer(source, schedule) as writer:
        writer.append_started(schedule[0])

    original = json.loads(source.read_bytes())
    for field, replacement, message in (
        ("protocol_sha256", "b" * 64, "foreign protocol"),
        (
            "execution_case_set_sha256",
            "e" * 64,
            "foreign execution case set",
        ),
        ("schedule_sha256", "c" * 64, "foreign schedule"),
        ("run_id", "run_foreign_v3", "foreign run"),
    ):
        document = dict(original)
        document[field] = replacement
        if field == "schedule_sha256":
            document["schedule_id"] = canonical_schedule_id_v3(replacement)
        if field in {"execution_case_set_sha256", "schedule_sha256", "run_id"}:
            document["checkpoint_id"] = _checkpoint_id(document)
        body = {key: value for key, value in document.items() if key != "record_sha256"}
        document["record_sha256"] = canonical_sha256(body)
        path = tmp_path / f"foreign-{field}.jsonl"
        path.write_bytes(canonical_json_bytes(document))
        with pytest.raises(CheckpointIntegrityErrorV3, match=message):
            _load(path, schedule)


def test_parser_rejects_reordering_and_duplicate_terminal_event(tmp_path: Path) -> None:
    schedule = _schedule()
    source = tmp_path / "source.jsonl"
    with _writer(source, schedule) as writer:
        writer.append_started(schedule[0])
        writer.append_completed(schedule[0], {"receipt": {"ok": True}})

    lines = source.read_bytes().splitlines()
    reordered = tmp_path / "reordered.jsonl"
    reordered.write_bytes(lines[1] + b"\n" + lines[0] + b"\n")
    with pytest.raises(CheckpointIntegrityErrorV3, match="sequence"):
        _load(reordered, schedule)

    terminal = json.loads(lines[1])
    duplicate = dict(terminal)
    duplicate["sequence"] = 2
    duplicate["previous_record_sha256"] = terminal["record_sha256"]
    body = {key: value for key, value in duplicate.items() if key != "record_sha256"}
    duplicate["record_sha256"] = canonical_sha256(body)
    duplicate_path = tmp_path / "duplicate-terminal.jsonl"
    duplicate_path.write_bytes(source.read_bytes() + canonical_json_bytes(duplicate))
    with pytest.raises(CheckpointIntegrityErrorV3, match="duplicate terminal"):
        _load(duplicate_path, schedule)


def _checkpoint_id(document: dict[str, object]) -> str:
    return "checkpoint_" + canonical_sha256(
        {
            "event": document["event"],
            "execution_case_set_sha256": document["execution_case_set_sha256"],
            "run_id": document["run_id"],
            "schedule_index": document["schedule_index"],
            "schedule_sha256": document["schedule_sha256"],
            "schema_version": "3.0",
        }
    )


def test_checkpoint_chain_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    schedule = _schedule()
    payload = {"receipt": {"known_cost_usd": "0.001", "ok": True}}
    paths = (tmp_path / "first.jsonl", tmp_path / "second.jsonl")
    for path in paths:
        with _writer(path, schedule) as writer:
            writer.append_started(schedule[0])
            writer.append_completed(schedule[0], payload)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    assert [record.event for record in _load(paths[0], schedule).records] == [
        CheckpointEventV3.STARTED,
        CheckpointEventV3.COMPLETED,
    ]
