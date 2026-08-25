"""Evaluation variant composition and stage timing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.contracts import ExecutionPlan, TaskStatus
from app.evaluation.runner import isolated_sample_database, load_corpus
from app.evaluation.v2_models import EvaluationVariantV2, RuntimeMode
from app.evaluation.variant_runtime import (
    EvaluationTurnError,
    build_variant_orchestrator_v2,
    observe_variant_turn_v2,
)
from app.orchestrator.progress import OrchestrationProgress
from app.orchestrator.schemas import OrchestrationResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_deterministic_variant_runs_real_agents_with_bounded_timings() -> None:
    case = load_corpus(PROJECT_ROOT / "evaluation" / "cases.v1.json").cases[0]
    variant = _deterministic_variant()

    with isolated_sample_database():
        observed = await observe_variant_turn_v2(
            build_variant_orchestrator_v2(variant, model_runtime=None),
            message=case.message,
            variant_id=variant.variant_id,
            case_id=case.case_id,
            session_id="sess_eval_runtime",
            request_id="req_eval_runtime",
            trace_id="trace_eval_runtime",
        )

    assert observed.result.status.value == "success"
    assert observed.result.model_calls == ()
    assert tuple(step.action for step in observed.result.plan.steps) == (
        "product.search",
    )
    latencies = observed.latencies
    assert latencies.routing_ms <= latencies.end_to_end_ms
    assert latencies.planning_ms <= latencies.end_to_end_ms
    assert latencies.agent_ms is not None
    assert latencies.agent_ms <= latencies.end_to_end_ms
    assert latencies.synthesis_ms <= latencies.end_to_end_ms


def test_variant_runtime_rejects_missing_or_hidden_model_runtime() -> None:
    deterministic = _deterministic_variant()
    hybrid = deterministic.model_copy(
        update={
            "variant_id": "hybrid_runtime_v2",
            "runtime_mode": RuntimeMode.HYBRID,
            "model_router_enabled": True,
            "max_model_calls": 1,
        }
    )

    with pytest.raises(ValueError, match="cannot receive"):
        build_variant_orchestrator_v2(deterministic, model_runtime=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="require a model runtime"):
        build_variant_orchestrator_v2(hybrid, model_runtime=None)


@pytest.mark.asyncio
async def test_turn_failure_does_not_expose_raw_exception_text() -> None:
    class FailingOrchestrator:
        async def run(self, **_: object) -> None:
            raise RuntimeError("sensitive-provider-payload")

    with pytest.raises(EvaluationTurnError) as captured:
        await observe_variant_turn_v2(  # type: ignore[arg-type]
            FailingOrchestrator(),
            message="Tìm sản phẩm.",
            variant_id="hybrid_full",
            case_id="simple_01_nova_search",
            session_id="sess_eval_failure",
            request_id="req_eval_failure",
            trace_id="trace_eval_failure",
        )

    assert "sensitive-provider-payload" not in str(captured.value)
    assert "hybrid_full" in str(captured.value)


@pytest.mark.asyncio
async def test_progress_events_project_exact_stage_wall_times() -> None:
    class ProgressOrchestrator:
        async def run(self, **arguments: object) -> OrchestrationResult:
            progress = arguments["progress"]
            assert callable(progress)
            for phase in (
                "routing.completed",
                "planning.completed",
                "agent.completed",
                "aggregation.completed",
            ):
                await progress(OrchestrationProgress(phase=phase, message=phase))
            return OrchestrationResult(
                status=TaskStatus.SUCCESS,
                answer="Grounded answer.",
                request_id="req_eval_timing",
                trace_id="trace_eval_timing",
                session_id="sess_eval_timing",
                intent="product.search",
                plan=ExecutionPlan(
                    plan_id="plan_eval_timing",
                    intent="product.search",
                ),
                duration_ms=100,
            )

    ticks = iter((0, 10_000_000, 30_000_000, 80_000_000, 100_000_000, 110_000_000))
    observed = await observe_variant_turn_v2(  # type: ignore[arg-type]
        ProgressOrchestrator(),
        message="Tìm sản phẩm.",
        variant_id="deterministic_runtime_v2",
        case_id="simple_01_nova_search",
        session_id="sess_eval_timing",
        request_id="req_eval_timing",
        trace_id="trace_eval_timing",
        clock_ns=lambda: next(ticks),
    )

    assert observed.latencies.routing_ms == 10
    assert observed.latencies.planning_ms == 20
    assert observed.latencies.agent_ms == 50
    assert observed.latencies.synthesis_ms == 20
    assert observed.latencies.end_to_end_ms == 110


def _deterministic_variant() -> EvaluationVariantV2:
    return EvaluationVariantV2(
        variant_id="deterministic_runtime_v2",
        description="Deterministic runtime test variant.",
        runtime_mode=RuntimeMode.DETERMINISTIC,
        enabled_agents=(
            "product_agent",
            "review_agent",
            "trust_agent",
            "market_agent",
        ),
        embedding_backend="hashing",
    )
