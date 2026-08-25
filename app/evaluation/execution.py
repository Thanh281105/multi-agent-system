"""Execute a complete frozen evaluation v2 matrix sequentially."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Callable

from app.evaluation.experiment import LoadedEvaluationExperimentV2
from app.evaluation.observation import build_evaluation_observation_v2
from app.evaluation.protocol import canonical_sha256
from app.evaluation.runner import isolated_sample_database
from app.evaluation.schedule import ScheduledTurnV2, build_evaluation_schedule_v2
from app.evaluation.v2_models import (
    EvaluationObservationV2,
    EvaluationProtocolV2,
    EvaluationVariantV2,
    RuntimeMode,
)
from app.evaluation.variant_runtime import (
    ClockNs,
    build_variant_orchestrator_v2,
    observe_variant_turn_v2,
)
from app.shared import ModelRuntime

ModelRuntimeFactoryV2 = Callable[[EvaluationVariantV2], ModelRuntime]
_RUN_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")


@dataclass(frozen=True, slots=True)
class EvaluationExecutionV2:
    protocol: EvaluationProtocolV2
    observations: tuple[EvaluationObservationV2, ...]
    sample_counts: dict[str, int]


async def run_evaluation_matrix_v2(
    *,
    run_id: str,
    assets: LoadedEvaluationExperimentV2,
    protocol: EvaluationProtocolV2,
    runtime_factory: ModelRuntimeFactoryV2 | None = None,
    clock_ns: ClockNs = perf_counter_ns,
) -> EvaluationExecutionV2:
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must be a lowercase stable identifier")
    _validate_protocol_assets(protocol, assets)
    cases = {case.case_id: case for case in assets.corpus.cases}
    selected_cases = {case_id: cases[case_id] for case_id in protocol.case_order}
    variants = {variant.variant_id: variant for variant in protocol.variants}
    runtimes: dict[str, ModelRuntime] = {}
    for variant in protocol.variants:
        if variant.runtime_mode != RuntimeMode.HYBRID:
            continue
        if runtime_factory is None:
            raise RuntimeError("hybrid evaluation requires a model runtime factory")
        runtimes[variant.variant_id] = runtime_factory(variant)

    observations: list[EvaluationObservationV2] = []
    with isolated_sample_database() as sample_counts:
        for turn in build_evaluation_schedule_v2(protocol):
            spec = selected_cases[turn.case_id]
            variant = variants[turn.variant_id]
            identifiers = _turn_identifiers(run_id, turn)
            orchestrator = build_variant_orchestrator_v2(
                variant,
                model_runtime=runtimes.get(variant.variant_id),
                failure_injection=spec.gold_case.failure_injection,
            )
            observed = await observe_variant_turn_v2(
                orchestrator,
                message=spec.gold_case.message,
                variant_id=variant.variant_id,
                case_id=spec.case_id,
                session_id=identifiers[0],
                request_id=identifiers[1],
                trace_id=identifiers[2],
                clock_ns=clock_ns,
            )
            if turn.is_warmup:
                continue
            if turn.phase is None or turn.execution_order is None:
                raise AssertionError("measurement turn lacks phase or order")
            observations.append(
                build_evaluation_observation_v2(
                    observed=observed,
                    spec=spec,
                    protocol=protocol,
                    variant=variant,
                    pricing=assets.pricing,
                    run_id=run_id,
                    phase=turn.phase,
                    repetition=turn.repetition,
                    execution_order=turn.execution_order,
                )
            )
    return EvaluationExecutionV2(
        protocol=protocol,
        observations=tuple(observations),
        sample_counts=dict(sample_counts),
    )


def _validate_protocol_assets(
    protocol: EvaluationProtocolV2,
    assets: LoadedEvaluationExperimentV2,
) -> None:
    if protocol.experiment_sha256 != assets.experiment_sha256:
        raise ValueError("protocol experiment hash does not match loaded assets")
    if protocol.corpus_sha256 != assets.corpus.corpus_sha256:
        raise ValueError("protocol corpus hash does not match loaded assets")
    if protocol.dataset_sha256 != assets.corpus.dataset_sha256:
        raise ValueError("protocol dataset hash does not match loaded assets")
    if protocol.pricing_sha256 != canonical_sha256(assets.pricing):
        raise ValueError("protocol pricing hash does not match loaded assets")
    known_case_ids = {case.case_id for case in assets.corpus.cases}
    if not set(protocol.case_order).issubset(known_case_ids):
        raise ValueError("protocol references cases absent from loaded assets")


def _turn_identifiers(
    run_id: str,
    turn: ScheduledTurnV2,
) -> tuple[str, str, str]:
    digest = hashlib.sha256(f"{run_id}\0{turn!r}".encode("utf-8")).hexdigest()[:24]
    return (
        f"sess_eval_{digest}",
        f"req_eval_{digest}",
        f"trace_eval_{digest}",
    )
