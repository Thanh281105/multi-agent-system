"""Map orchestration evidence into a scored, billable evaluation observation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.contracts import AgentResult, TaskStatus
from app.evaluation.metrics import score_case
from app.evaluation.models import EvaluationObservation
from app.evaluation.protocol import (
    estimate_observation_cost,
    protocol_sha256,
    validate_observation_protocol,
)
from app.evaluation.v2_models import (
    EvaluationCaseSpecV2,
    EvaluationObservationV2,
    EvaluationPhase,
    EvaluationProtocolV2,
    EvaluationVariantV2,
    ModelCallOutcome,
    ModelCallV2,
    ModelStage,
    PlanEdgeV2,
    PricingManifestV2,
    TokenUsageV2,
)
from app.evaluation.variant_runtime import ObservedOrchestrationTurnV2
from app.shared import ModelCallMetadata

_STAGE_MAP = {
    "routing": ModelStage.ROUTING,
    "planning": ModelStage.PLANNING,
    "specialist.analysis": ModelStage.SPECIALIST,
    "synthesis": ModelStage.SYNTHESIS,
}


def build_evaluation_observation_v2(
    *,
    observed: ObservedOrchestrationTurnV2,
    spec: EvaluationCaseSpecV2,
    protocol: EvaluationProtocolV2,
    variant: EvaluationVariantV2,
    pricing: PricingManifestV2,
    run_id: str,
    phase: EvaluationPhase,
    repetition: int,
    execution_order: int,
) -> EvaluationObservationV2:
    protocol_variant = next(
        (
            configured
            for configured in protocol.variants
            if configured.variant_id == variant.variant_id
        ),
        None,
    )
    if protocol_variant != variant:
        raise ValueError("observation variant does not match the frozen protocol")
    result = observed.result
    model_calls = tuple(_model_call(call) for call in result.model_calls)
    provenance_source_ids = _unique(record.source_id for record in result.provenance)
    error_codes = _unique(
        (
            error.code
            for agent_result in result.agent_results
            for error in agent_result.errors
        ),
        (call.error_code for call in model_calls if call.error_code is not None),
    )
    actions = tuple(step.action for step in result.plan.steps)
    retrieved_ids = retrieved_product_ids(result.agent_results)
    legacy = EvaluationObservation(
        system_id=("multi_agent_real" if model_calls else "multi_agent_offline"),
        case_id=spec.case_id,
        category=spec.gold_case.category,
        repetition=repetition,
        status=result.status,
        predicted_intent=result.intent,
        actions=actions,
        retrieved_product_ids=retrieved_ids,
        selected_product_id=result.selected_product_id,
        answer=result.answer,
        error_codes=error_codes,
        provenance_count=len(provenance_source_ids),
        agent_attempts=len(result.agent_results),
        agent_failures=sum(
            agent_result.status == TaskStatus.FAILED
            for agent_result in result.agent_results
        ),
        latency_ms=observed.latencies.end_to_end_ms,
        token_usage=sum(
            call.usage.total_tokens for call in model_calls if call.usage is not None
        ),
    )
    score = score_case(spec.gold_case, legacy)
    provisional = EvaluationObservationV2(
        run_id=run_id,
        protocol_sha256=protocol_sha256(protocol),
        variant_id=variant.variant_id,
        case_id=spec.case_id,
        category=spec.gold_case.category.value,
        phase=phase,
        repetition=repetition,
        execution_order=execution_order,
        random_seed=protocol.random_seed,
        robustness_policy=spec.robustness_policy,
        parent_case_id=spec.parent_case_id,
        transform_id=spec.transform_id,
        status=result.status,
        predicted_intent=result.intent,
        actions=actions,
        dependency_edges=tuple(
            PlanEdgeV2(
                source_step_id=dependency,
                target_step_id=step.step_id,
            )
            for step in result.plan.steps
            for dependency in step.depends_on
        ),
        retrieved_product_ids=retrieved_ids,
        selected_product_id=result.selected_product_id,
        provenance_source_ids=provenance_source_ids,
        answer=result.answer,
        assertions_passed=score.assertions_passed,
        assertion_count=score.assertion_count,
        routing_correct=score.routing_correct,
        exact_plan=score.exact_plan,
        task_success=score.task_success,
        refusal_or_incomplete=(
            result.intent == "general.unsupported"
            or result.status in {TaskStatus.PARTIAL_SUCCESS, TaskStatus.FAILED}
        ),
        end_to_end_latency_ms=observed.latencies.end_to_end_ms,
        routing_latency_ms=observed.latencies.routing_ms,
        planning_latency_ms=observed.latencies.planning_ms,
        agent_latency_ms=observed.latencies.agent_ms,
        synthesis_latency_ms=observed.latencies.synthesis_ms,
        retrieval_f1=score.retrieval_f1,
        model_calls=model_calls,
        error_codes=error_codes,
    )
    cost = estimate_observation_cost(provisional, pricing)
    observation = EvaluationObservationV2.model_validate(
        {
            **provisional.model_dump(),
            "estimated_cost_usd": cost.value_usd,
            "cost_unavailable_reason": cost.unavailable_reason,
        }
    )
    validate_observation_protocol(observation, protocol)
    return observation


def retrieved_product_ids(results: Sequence[AgentResult]) -> tuple[int, ...]:
    product_ids: list[int] = []

    def append(value: object) -> None:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value not in product_ids
        ):
            product_ids.append(value)

    for result in results:
        data = result.data
        for key in ("products", "analyses"):
            values = data.get(key)
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict):
                        append(item.get("id", item.get("product_id")))
        for key in ("ranking", "search"):
            container = data.get(key)
            if isinstance(container, dict):
                values = container.get("products")
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, dict):
                            append(item.get("id"))
        retrieval = data.get("retrieval")
        if isinstance(retrieval, dict):
            product = retrieval.get("product")
            if isinstance(product, dict):
                append(product.get("id"))
    return tuple(product_ids)


def _model_call(metadata: ModelCallMetadata) -> ModelCallV2:
    try:
        stage = _STAGE_MAP[metadata.stage]
    except KeyError:
        raise ValueError(
            f"unknown evaluation model stage: {metadata.stage!r}"
        ) from None
    outcome = (
        ModelCallOutcome.FALLBACK
        if metadata.fallback_used
        else (
            ModelCallOutcome.SUCCESS
            if metadata.status == "success"
            else ModelCallOutcome.ERROR
        )
    )
    usage = (
        TokenUsageV2(
            input_tokens=metadata.input_tokens,
            cached_input_tokens=metadata.cached_input_tokens,
            output_tokens=metadata.output_tokens,
            reasoning_tokens=metadata.reasoning_tokens,
            total_tokens=metadata.total_tokens,
        )
        if metadata.status == "success"
        else None
    )
    return ModelCallV2(
        call_id=metadata.call_id,
        stage=stage,
        provider=metadata.provider,
        model=metadata.model,
        outcome=outcome,
        attempt=metadata.attempts,
        latency_ms=metadata.duration_ms,
        usage=usage,
        response_id=metadata.response_id,
        error_code=(metadata.error_code if metadata.status == "failed" else None),
        fallback_reason=metadata.fallback_reason,
    )


def _unique(*values: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for group in values:
        for value in group:
            if value not in unique:
                unique.append(value)
    return tuple(unique)
