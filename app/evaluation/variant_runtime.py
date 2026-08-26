"""Compose and time one frozen evaluation variant without provider leakage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Callable

from app.agent_gateway import AgentGateway
from app.agents import build_default_dispatcher
from app.evaluation.failure import FailureInjectingDispatcher
from app.evaluation.models import FailureInjection
from app.evaluation.retrieval import search_evaluation_sample_knowledge_v2
from app.evaluation.v2_models import (
    EvaluationVariantV2,
    ModelBindingV2,
    ModelStage,
    RuntimeMode,
)
from app.mcp.catalog import build_default_mcp_router
from app.orchestrator import MultiAgentOrchestrator
from app.orchestrator.aggregator import ResultAggregator
from app.orchestrator.planner import ExecutionPlanner
from app.orchestrator.progress import OrchestrationProgress
from app.orchestrator.router import IntentRouter
from app.orchestrator.schemas import OrchestrationResult
from app.shared import ModelRuntime, ModelRuntimeMode, ReasoningEffort

ClockNs = Callable[[], int]


class EvaluationTurnError(RuntimeError):
    """Stable failure that never includes a provider payload or prompt."""


@dataclass(frozen=True, slots=True)
class StageLatenciesV2:
    routing_ms: float
    planning_ms: float
    agent_ms: float | None
    synthesis_ms: float
    end_to_end_ms: float


@dataclass(frozen=True, slots=True)
class ObservedOrchestrationTurnV2:
    result: OrchestrationResult
    latencies: StageLatenciesV2


def build_variant_orchestrator_v2(
    variant: EvaluationVariantV2,
    *,
    model_runtime: ModelRuntime | None,
    failure_injection: FailureInjection | None = None,
) -> MultiAgentOrchestrator:
    if variant.runtime_mode == RuntimeMode.DETERMINISTIC and model_runtime is not None:
        raise ValueError("deterministic variants cannot receive a model runtime")
    if variant.runtime_mode == RuntimeMode.HYBRID and model_runtime is None:
        raise ValueError("hybrid variants require a model runtime")
    if variant.runtime_mode not in {RuntimeMode.DETERMINISTIC, RuntimeMode.HYBRID}:
        raise ValueError("captured variants are not executable")

    runtime_mode: ModelRuntimeMode = (
        "required" if variant.fallback_policy == "fail_closed" else "hybrid"
    )
    gateway = AgentGateway(
        router=build_default_mcp_router(
            knowledge_search=search_evaluation_sample_knowledge_v2
        )
    )
    specialist_binding = _binding(variant, ModelStage.SPECIALIST)
    dispatcher = build_default_dispatcher(
        gateway,
        enabled_agents=variant.enabled_agents,
        model_runtime=(model_runtime if specialist_binding is not None else None),
        runtime_mode=(runtime_mode if specialist_binding is not None else "off"),
        specialist_model=(
            specialist_binding.model
            if specialist_binding is not None
            else "gpt-5.4-nano"
        ),
        reasoning_effort=_reasoning_effort(specialist_binding),
    )
    if failure_injection is not None:
        dispatcher = FailureInjectingDispatcher(dispatcher, failure_injection)

    routing_binding = _binding(variant, ModelStage.ROUTING)
    planning_binding = _binding(variant, ModelStage.PLANNING)
    synthesis_binding = _binding(variant, ModelStage.SYNTHESIS)
    return MultiAgentOrchestrator(
        dispatcher=dispatcher,
        router=IntentRouter(
            model_runtime=(model_runtime if routing_binding is not None else None),
            runtime_mode=(runtime_mode if routing_binding is not None else "off"),
            model=(
                routing_binding.model if routing_binding is not None else "gpt-5.4-nano"
            ),
            reasoning_effort=_reasoning_effort(routing_binding),
        ),
        planner=ExecutionPlanner(
            model_runtime=(model_runtime if planning_binding is not None else None),
            runtime_mode=(runtime_mode if planning_binding is not None else "off"),
            model=(
                planning_binding.model
                if planning_binding is not None
                else "gpt-5.4-nano"
            ),
            reasoning_effort=_reasoning_effort(planning_binding),
        ),
        aggregator=ResultAggregator(
            model_runtime=(model_runtime if synthesis_binding is not None else None),
            runtime_mode=(runtime_mode if synthesis_binding is not None else "off"),
            model=(
                synthesis_binding.model
                if synthesis_binding is not None
                else "gpt-5.4-mini"
            ),
            reasoning_effort=_reasoning_effort(synthesis_binding),
        ),
    )


async def observe_variant_turn_v2(
    orchestrator: MultiAgentOrchestrator,
    *,
    message: str,
    variant_id: str,
    case_id: str,
    session_id: str,
    request_id: str,
    trace_id: str,
    clock_ns: ClockNs = perf_counter_ns,
) -> ObservedOrchestrationTurnV2:
    timeline = _ProgressTimeline(clock_ns)
    started_at = clock_ns()
    try:
        result = await orchestrator.run(
            message=message,
            principal_id="evaluation_runner",
            session_id=session_id,
            request_id=request_id,
            trace_id=trace_id,
            progress=timeline.record,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise EvaluationTurnError(
            f"evaluation turn failed for variant {variant_id!r}, case {case_id!r}"
        ) from None
    finished_at = clock_ns()
    return ObservedOrchestrationTurnV2(
        result=result,
        latencies=timeline.latencies(started_at, finished_at),
    )


class _ProgressTimeline:
    def __init__(self, clock_ns: ClockNs) -> None:
        self._clock_ns = clock_ns
        self._routing_completed: int | None = None
        self._planning_completed: int | None = None
        self._last_agent_completed: int | None = None
        self._aggregation_completed: int | None = None

    async def record(self, event: OrchestrationProgress) -> None:
        timestamp = self._clock_ns()
        if event.phase == "routing.completed":
            self._routing_completed = timestamp
        elif event.phase == "planning.completed":
            self._planning_completed = timestamp
        elif event.phase == "agent.completed":
            self._last_agent_completed = timestamp
        elif event.phase == "aggregation.completed":
            self._aggregation_completed = timestamp

    def latencies(self, started_at: int, finished_at: int) -> StageLatenciesV2:
        if (
            self._routing_completed is None
            or self._planning_completed is None
            or self._aggregation_completed is None
        ):
            raise EvaluationTurnError("evaluation progress timeline is incomplete")
        agent_ms = (
            _milliseconds(self._last_agent_completed - self._planning_completed)
            if self._last_agent_completed is not None
            else None
        )
        synthesis_started = (
            self._last_agent_completed
            if self._last_agent_completed is not None
            else self._planning_completed
        )
        return StageLatenciesV2(
            routing_ms=_milliseconds(self._routing_completed - started_at),
            planning_ms=_milliseconds(
                self._planning_completed - self._routing_completed
            ),
            agent_ms=agent_ms,
            synthesis_ms=_milliseconds(self._aggregation_completed - synthesis_started),
            end_to_end_ms=_milliseconds(finished_at - started_at),
        )


def _binding(
    variant: EvaluationVariantV2,
    stage: ModelStage,
) -> ModelBindingV2 | None:
    return next(
        (binding for binding in variant.model_bindings if binding.stage == stage),
        None,
    )


def _reasoning_effort(binding: ModelBindingV2 | None) -> ReasoningEffort:
    return binding.reasoning_effort.value if binding is not None else "low"


def _milliseconds(duration_ns: int) -> float:
    if duration_ns < 0:
        raise EvaluationTurnError("evaluation clock moved backwards")
    return duration_ns / 1_000_000
