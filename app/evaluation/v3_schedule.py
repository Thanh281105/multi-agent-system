"""Deterministic Package 7 pilot schedule and canonical turn identities."""

from __future__ import annotations

import random
import re
from typing import Literal

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_models import (
    EvaluationProtocolV3,
    ObservationIdentityV3,
    ScheduledTurnKindV3,
    ScheduledTurnV3,
    canonical_observation_id_v3,
    canonical_turn_id_v3,
)

SCHEDULE_ALGORITHM_ID_V3: Literal["package7_pilot_interleaved_v1"] = (
    "package7_pilot_interleaved_v1"
)
_WARMUP_SEED_MASK = 0xA11CE
_MEASUREMENT_SEED_MASK = 0xE7A13
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

_SCHEDULE_ALGORITHM_DESCRIPTOR = {
    "algorithm_id": SCHEDULE_ALGORITHM_ID_V3,
    "identity_encoding": "canonical_json_sha256",
    "warmup_seed_mask": _WARMUP_SEED_MASK,
    "measurement_seed_mask": _MEASUREMENT_SEED_MASK,
    "warmup_ordering": "seeded_variant_shuffle",
    "measurement_ordering": "case_then_repeat_then_seeded_variant_shuffle",
    "warmup_execution_order": None,
    "measured_execution_order": "contiguous_from_zero",
}


def schedule_algorithm_sha256_v3() -> str:
    return canonical_sha256(_SCHEDULE_ALGORITHM_DESCRIPTOR)


def build_pilot_schedule_v3(
    protocol: EvaluationProtocolV3,
    *,
    run_id: str,
    protocol_sha256: str,
) -> tuple[ScheduledTurnV3, ...]:
    """Build four warmups followed by all 32 interleaved pilot cells."""

    if _SHA256.fullmatch(protocol_sha256) is None:
        raise ValueError("protocol_sha256 must be a lowercase SHA-256 digest")
    from app.evaluation.v3_protocol import evaluation_protocol_sha256_v3

    if protocol_sha256 != evaluation_protocol_sha256_v3(protocol):
        raise ValueError("schedule protocol hash does not match the frozen protocol")

    case_bindings = {case.case_id: case for case in protocol.pilot_cases}
    warmup_case = case_bindings[protocol.experiment.warmup_case_id]
    variant_ids = tuple(variant.variant_id for variant in protocol.variants)
    turns: list[ScheduledTurnV3] = []

    warmup_generator = random.Random(protocol.random_seed ^ _WARMUP_SEED_MASK)
    for variant_id in _shuffled(variant_ids, warmup_generator):
        identity = ObservationIdentityV3(
            run_id=run_id,
            protocol_sha256=protocol_sha256,
            schedule_algorithm_id=SCHEDULE_ALGORITHM_ID_V3,
            turn_kind=ScheduledTurnKindV3.WARMUP,
            variant_id=variant_id,
            case_id=warmup_case.case_id,
            work_group_id=warmup_case.work_group_id,
            repetition=0,
        )
        turns.append(
            ScheduledTurnV3(
                turn_id=canonical_turn_id_v3(identity),
                identity=identity,
                schedule_index=len(turns),
            )
        )

    execution_order = 0
    measurement_generator = random.Random(protocol.random_seed ^ _MEASUREMENT_SEED_MASK)
    for case in protocol.pilot_cases:
        for repetition in range(protocol.experiment.pilot_repeats):
            for variant_id in _shuffled(variant_ids, measurement_generator):
                identity = ObservationIdentityV3(
                    run_id=run_id,
                    protocol_sha256=protocol_sha256,
                    schedule_algorithm_id=SCHEDULE_ALGORITHM_ID_V3,
                    turn_kind=ScheduledTurnKindV3.MEASURED,
                    variant_id=variant_id,
                    case_id=case.case_id,
                    work_group_id=case.work_group_id,
                    repetition=repetition,
                )
                turns.append(
                    ScheduledTurnV3(
                        turn_id=canonical_turn_id_v3(identity),
                        observation_id=canonical_observation_id_v3(identity),
                        identity=identity,
                        schedule_index=len(turns),
                        execution_order=execution_order,
                    )
                )
                execution_order += 1
    return tuple(turns)


def pilot_schedule_sha256_v3(schedule: tuple[ScheduledTurnV3, ...]) -> str:
    return canonical_sha256([turn.model_dump(mode="json") for turn in schedule])


def _shuffled[T](values: tuple[T, ...], generator: random.Random) -> tuple[T, ...]:
    shuffled = list(values)
    generator.shuffle(shuffled)
    return tuple(shuffled)
