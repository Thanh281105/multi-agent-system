"""Deterministic paired/interleaved schedules for evaluation v2."""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.evaluation.v2_models import EvaluationPhase, EvaluationProtocolV2

_WARMUP_SEED_MASK = 0xA11CE
_CORRECTNESS_SEED_MASK = 0xC011EC7
_LATENCY_SEED_MASK = 0x1A7E


@dataclass(frozen=True, slots=True)
class ScheduledTurnV2:
    case_id: str
    variant_id: str
    phase: EvaluationPhase | None
    repetition: int
    is_warmup: bool
    execution_order: int | None


def build_evaluation_schedule_v2(
    protocol: EvaluationProtocolV2,
) -> tuple[ScheduledTurnV2, ...]:
    """Build a reproducible schedule that interleaves variants within each pair."""

    variant_ids = tuple(variant.variant_id for variant in protocol.variants)
    schedule: list[ScheduledTurnV2] = []
    if protocol.warmup_repeats:
        if protocol.warmup_case_id is None:
            raise AssertionError("validated protocol omitted its warmup case")
        warmup_generator = random.Random(protocol.random_seed ^ _WARMUP_SEED_MASK)
        for repetition in range(protocol.warmup_repeats):
            for variant_id in _shuffled(variant_ids, warmup_generator):
                schedule.append(
                    ScheduledTurnV2(
                        case_id=protocol.warmup_case_id,
                        variant_id=variant_id,
                        phase=None,
                        repetition=repetition,
                        is_warmup=True,
                        execution_order=None,
                    )
                )

    execution_order = 0
    execution_order = _append_measurements(
        schedule,
        phase=EvaluationPhase.CORRECTNESS,
        case_ids=protocol.case_order,
        repeats=protocol.correctness_repeats,
        variant_ids=variant_ids,
        random_seed=protocol.random_seed ^ _CORRECTNESS_SEED_MASK,
        execution_order=execution_order,
    )
    _append_measurements(
        schedule,
        phase=EvaluationPhase.LATENCY,
        case_ids=protocol.effective_latency_case_order,
        repeats=protocol.latency_repeats,
        variant_ids=variant_ids,
        random_seed=protocol.random_seed ^ _LATENCY_SEED_MASK,
        execution_order=execution_order,
    )
    return tuple(schedule)


def _append_measurements(
    schedule: list[ScheduledTurnV2],
    *,
    phase: EvaluationPhase,
    case_ids: tuple[str, ...],
    repeats: int,
    variant_ids: tuple[str, ...],
    random_seed: int,
    execution_order: int,
) -> int:
    generator = random.Random(random_seed)
    for case_id in case_ids:
        for repetition in range(repeats):
            for variant_id in _shuffled(variant_ids, generator):
                schedule.append(
                    ScheduledTurnV2(
                        case_id=case_id,
                        variant_id=variant_id,
                        phase=phase,
                        repetition=repetition,
                        is_warmup=False,
                        execution_order=execution_order,
                    )
                )
                execution_order += 1
    return execution_order


def _shuffled(values: tuple[str, ...], generator: random.Random) -> tuple[str, ...]:
    shuffled = list(values)
    generator.shuffle(shuffled)
    return tuple(shuffled)
