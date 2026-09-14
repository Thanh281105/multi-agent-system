"""Package 7 deterministic pilot schedule and identity tests."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_models import (
    PACKAGE7_PILOT_CASE_ORDER,
    PACKAGE7_VARIANT_ORDER,
    EvaluationAssetBindingsV3,
    EvaluationExperimentConfigV3,
    ScheduledTurnKindV3,
    ScheduledTurnV3,
)
from app.evaluation.v3_protocol import (
    LoadedEvaluationExperimentV3,
    build_evaluation_protocol_v3,
    evaluation_protocol_sha256_v3,
    load_evaluation_experiment_v3,
)
from app.evaluation.v3_schedule import (
    build_pilot_schedule_v3,
    pilot_schedule_sha256_v3,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_PATH = PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"


def test_pilot_schedule_has_four_warmups_and_every_8x4_cell_once() -> None:
    protocol = _protocol()
    schedule = build_pilot_schedule_v3(
        protocol,
        run_id="run_package7_pilot",
        protocol_sha256=evaluation_protocol_sha256_v3(protocol),
    )

    warmups = [
        turn
        for turn in schedule
        if turn.identity.turn_kind == ScheduledTurnKindV3.WARMUP
    ]
    measured = [
        turn
        for turn in schedule
        if turn.identity.turn_kind == ScheduledTurnKindV3.MEASURED
    ]
    assert len(schedule) == 36
    assert len(warmups) == 4
    assert len(measured) == 32
    assert {turn.identity.variant_id for turn in warmups} == set(PACKAGE7_VARIANT_ORDER)
    assert {turn.identity.case_id for turn in warmups} == {
        protocol.experiment.warmup_case_id
    }
    assert all(turn.observation_id is None for turn in warmups)
    assert all(turn.execution_order is None for turn in warmups)
    assert [turn.execution_order for turn in measured] == list(range(32))

    cells = {
        (
            turn.identity.case_id,
            turn.identity.variant_id,
            turn.identity.repetition,
        )
        for turn in measured
    }
    assert cells == {
        (case_id, variant_id, 0)
        for case_id in PACKAGE7_PILOT_CASE_ORDER
        for variant_id in PACKAGE7_VARIANT_ORDER
    }
    blocks: dict[str, set[str]] = defaultdict(set)
    for turn in measured:
        blocks[turn.identity.case_id].add(turn.identity.variant_id)
    assert tuple(blocks) == PACKAGE7_PILOT_CASE_ORDER
    assert all(variants == set(PACKAGE7_VARIANT_ORDER) for variants in blocks.values())


def test_schedule_and_canonical_identities_are_reproducible() -> None:
    protocol = _protocol()
    protocol_hash = evaluation_protocol_sha256_v3(protocol)

    first = build_pilot_schedule_v3(
        protocol,
        run_id="run_package7_stable",
        protocol_sha256=protocol_hash,
    )
    second = build_pilot_schedule_v3(
        protocol,
        run_id="run_package7_stable",
        protocol_sha256=protocol_hash,
    )

    assert first == second
    assert pilot_schedule_sha256_v3(first) == pilot_schedule_sha256_v3(second)
    assert len({turn.turn_id for turn in first}) == len(first)
    assert (
        len({turn.observation_id for turn in first if turn.observation_id is not None})
        == 32
    )
    for turn in first:
        digest = canonical_sha256(turn.identity)
        assert turn.turn_id == f"turn_{digest}"
        if turn.identity.turn_kind == ScheduledTurnKindV3.MEASURED:
            assert turn.observation_id == f"obs_{digest}"

    another_run = build_pilot_schedule_v3(
        protocol,
        run_id="run_package7_other",
        protocol_sha256=protocol_hash,
    )
    assert {turn.turn_id for turn in first}.isdisjoint(
        turn.turn_id for turn in another_run
    )


def test_schedule_rejects_false_or_malformed_protocol_hashes() -> None:
    protocol = _protocol()

    with pytest.raises(ValueError, match="does not match the frozen protocol"):
        build_pilot_schedule_v3(
            protocol,
            run_id="run_package7_false_hash",
            protocol_sha256="f" * 64,
        )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        build_pilot_schedule_v3(
            protocol,
            run_id="run_package7_bad_hash",
            protocol_sha256="not-a-sha256",
        )


def test_scheduled_turn_rejects_tampered_canonical_ids() -> None:
    protocol = _protocol()
    schedule = build_pilot_schedule_v3(
        protocol,
        run_id="run_package7_turn_integrity",
        protocol_sha256=evaluation_protocol_sha256_v3(protocol),
    )

    with pytest.raises(ValidationError, match="turn ID"):
        ScheduledTurnV3.model_validate(
            {**schedule[0].model_dump(), "turn_id": "turn_tampered_identity"}
        )
    measured = next(turn for turn in schedule if turn.observation_id is not None)
    with pytest.raises(ValidationError, match="observation ID"):
        ScheduledTurnV3.model_validate(
            {**measured.model_dump(), "observation_id": "obs_tampered_identity"}
        )


def test_warmup_binding_does_not_perturb_measured_structural_order() -> None:
    loaded = load_evaluation_experiment_v3(EXPERIMENT_PATH)
    original = build_evaluation_protocol_v3(loaded, assets=_assets())
    changed_payload = loaded.config.model_dump(mode="json")
    changed_payload["warmup_case_id"] = PACKAGE7_PILOT_CASE_ORDER[1]
    changed_config = EvaluationExperimentConfigV3.model_validate(changed_payload)
    changed = build_evaluation_protocol_v3(
        LoadedEvaluationExperimentV3(
            config=changed_config,
            experiment_sha256=canonical_sha256(changed_config),
        ),
        assets=_assets(),
    )

    original_schedule = build_pilot_schedule_v3(
        original,
        run_id="run_package7_warmup",
        protocol_sha256=evaluation_protocol_sha256_v3(original),
    )
    changed_schedule = build_pilot_schedule_v3(
        changed,
        run_id="run_package7_warmup",
        protocol_sha256=evaluation_protocol_sha256_v3(changed),
    )

    assert _measured_structure(original_schedule) == _measured_structure(
        changed_schedule
    )


def _protocol():
    return build_evaluation_protocol_v3(
        load_evaluation_experiment_v3(EXPERIMENT_PATH),
        assets=_assets(),
    )


def _assets() -> EvaluationAssetBindingsV3:
    return EvaluationAssetBindingsV3.model_validate(
        {
            field_name: f"{index + 1:064x}"
            for index, field_name in enumerate(EvaluationAssetBindingsV3.model_fields)
        }
    )


def _measured_structure(schedule):
    return tuple(
        (
            turn.identity.variant_id,
            turn.identity.case_id,
            turn.identity.work_group_id,
            turn.identity.repetition,
            turn.execution_order,
        )
        for turn in schedule
        if turn.identity.turn_kind == ScheduledTurnKindV3.MEASURED
    )
