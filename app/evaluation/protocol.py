"""Canonical protocol hashing and versioned provider-cost accounting."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.evaluation.v2_models import (
    ComparisonMetric,
    CostEstimateV2,
    EvaluationObservationV2,
    EvaluationPhase,
    EvaluationProtocolV2,
    ModelCallV2,
    PricingManifestV2,
)

_ONE_MILLION = Decimal(1_000_000)
_COST_PRECISION = Decimal("0.000000000001")
_CORRECTNESS_COMPARISON_METRICS = (
    ComparisonMetric.TASK_SUCCESS,
    ComparisonMetric.ROUTING_CORRECT,
    ComparisonMetric.EXACT_PLAN,
    ComparisonMetric.ANSWER_ASSERTION_ACCURACY,
    ComparisonMetric.RETRIEVAL_F1,
    ComparisonMetric.TOTAL_TOKENS,
    ComparisonMetric.ESTIMATED_COST_USD,
)

ComparisonKeyV2 = tuple[str, str, ComparisonMetric, EvaluationPhase]


def canonical_json_bytes(value: BaseModel | dict[str, Any] | list[Any]) -> bytes:
    payload: object = (
        value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    )
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def canonical_sha256(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def protocol_sha256(protocol: EvaluationProtocolV2) -> str:
    return canonical_sha256(protocol)


def comparison_pairs_v2(
    protocol: EvaluationProtocolV2,
) -> tuple[tuple[str, str], ...]:
    """Return the protocol-owned baseline and parent ablation pairs."""

    baseline_id = protocol.baseline_variant_id
    pairs: list[tuple[str, str]] = []
    for candidate in protocol.variants:
        if candidate.variant_id == baseline_id:
            continue
        pairs.append((baseline_id, candidate.variant_id))
        if (
            candidate.parent_variant_id is not None
            and candidate.parent_variant_id != baseline_id
        ):
            pairs.append((candidate.parent_variant_id, candidate.variant_id))
    return tuple(dict.fromkeys(pairs))


def expected_comparison_keys_v2(
    protocol: EvaluationProtocolV2,
) -> tuple[ComparisonKeyV2, ...]:
    """Return every analysis cell required by active protocol phases."""

    phase_metrics: list[tuple[EvaluationPhase, tuple[ComparisonMetric, ...]]] = []
    if protocol.correctness_repeats:
        phase_metrics.append(
            (EvaluationPhase.CORRECTNESS, _CORRECTNESS_COMPARISON_METRICS)
        )
    if protocol.latency_repeats:
        phase_metrics.append(
            (
                EvaluationPhase.LATENCY,
                (ComparisonMetric.END_TO_END_LATENCY_MS,),
            )
        )
    return tuple(
        (baseline_id, candidate_id, metric, phase)
        for baseline_id, candidate_id in comparison_pairs_v2(protocol)
        for phase, metrics in phase_metrics
        for metric in metrics
    )


def comparison_seed_v2(
    random_seed: int,
    baseline_id: str,
    candidate_id: str,
    metric: ComparisonMetric,
    phase: EvaluationPhase,
) -> int:
    """Derive one stable bootstrap seed for an analysis matrix cell."""

    payload = (
        f"{random_seed}\0{baseline_id}\0{candidate_id}\0{metric.value}\0{phase.value}"
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big")


def estimate_model_call_cost(
    call: ModelCallV2,
    pricing: PricingManifestV2,
) -> CostEstimateV2:
    if call.provider != pricing.provider:
        return CostEstimateV2(
            unavailable_reason=(
                f"pricing provider {pricing.provider!r} does not cover "
                f"call provider {call.provider!r}"
            )
        )
    if call.usage is None:
        return CostEstimateV2(
            unavailable_reason=f"model call {call.call_id!r} has no token usage"
        )
    entry = next((item for item in pricing.entries if item.model == call.model), None)
    if entry is None:
        return CostEstimateV2(
            unavailable_reason=f"pricing manifest has no entry for {call.model!r}"
        )

    usage = call.usage
    uncached_input = usage.input_tokens - usage.cached_input_tokens
    value = (
        Decimal(uncached_input) * entry.input_per_million_usd
        + Decimal(usage.cached_input_tokens) * entry.cached_input_per_million_usd
        + Decimal(usage.output_tokens) * entry.output_per_million_usd
    ) / _ONE_MILLION
    return CostEstimateV2(value_usd=value.quantize(_COST_PRECISION))


def estimate_observation_cost(
    observation: EvaluationObservationV2,
    pricing: PricingManifestV2,
) -> CostEstimateV2:
    estimates = [
        estimate_model_call_cost(call, pricing) for call in observation.model_calls
    ]
    unavailable = [
        estimate.unavailable_reason
        for estimate in estimates
        if estimate.unavailable_reason is not None
    ]
    if unavailable:
        return CostEstimateV2(unavailable_reason="; ".join(unavailable))
    total = sum(
        (estimate.value_usd or Decimal(0) for estimate in estimates),
        start=Decimal(0),
    )
    return CostEstimateV2(value_usd=total.quantize(_COST_PRECISION))


def validate_observation_protocol(
    observation: EvaluationObservationV2,
    protocol: EvaluationProtocolV2,
) -> None:
    expected_hash = protocol_sha256(protocol)
    if observation.protocol_sha256 != expected_hash:
        raise ValueError("observation protocol hash does not match protocol")
    variant = next(
        (
            item
            for item in protocol.variants
            if item.variant_id == observation.variant_id
        ),
        None,
    )
    if variant is None:
        raise ValueError(
            f"observation references unknown variant: {observation.variant_id}"
        )
    if observation.case_id not in set(protocol.case_order):
        raise ValueError(f"observation references unknown case: {observation.case_id}")
    if observation.phase == EvaluationPhase.LATENCY and observation.case_id not in set(
        protocol.effective_latency_case_order
    ):
        raise ValueError("latency observation references a non-latency case")
    repeat_limit = (
        protocol.correctness_repeats
        if observation.phase == EvaluationPhase.CORRECTNESS
        else protocol.latency_repeats
    )
    if observation.repetition >= repeat_limit:
        raise ValueError(
            f"observation repetition exceeds {observation.phase.value} protocol limit"
        )
    if len(observation.model_calls) > variant.max_model_calls:
        raise ValueError("observation exceeds variant model-call limit")
    bindings = {binding.stage: binding for binding in variant.model_bindings}
    for call in observation.model_calls:
        binding = bindings.get(call.stage)
        if binding is None:
            raise ValueError(
                f"observation uses disabled model stage: {call.stage.value}"
            )
        if (call.provider, call.model) != (binding.provider, binding.model):
            raise ValueError(
                f"observation model does not match {call.stage.value} binding"
            )
