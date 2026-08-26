"""Orchestration-to-evaluation observation mapping tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts import (
    AgentResult,
    DataProvenance,
    ExecutionPlan,
    ExecutionStep,
    TaskStatus,
)
from app.evaluation.corpus import load_evaluation_corpus_v2
from app.evaluation.observation import build_evaluation_observation_v2
from app.evaluation.protocol import canonical_sha256, estimate_observation_cost
from app.evaluation.v2_models import (
    EvaluationPhase,
    EvaluationProtocolV2,
    EvaluationVariantV2,
    ModelBindingV2,
    ModelCallOutcome,
    ModelPriceV2,
    ModelRuntimePolicyV2,
    ModelStage,
    PricingManifestV2,
    RuntimeMode,
)
from app.evaluation.variant_runtime import (
    ObservedOrchestrationTurnV2,
    StageLatenciesV2,
)
from app.orchestrator.schemas import OrchestrationResult
from app.shared import ModelCallMetadata

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_observation_maps_plan_evidence_usage_scores_and_exact_cost() -> None:
    pricing = _pricing()
    protocol = _protocol(pricing)
    variant = protocol.variants[1]
    spec = load_evaluation_corpus_v2(
        PROJECT_ROOT / "evaluation" / "corpus.v2.json",
        PROJECT_ROOT / "evaluation" / "cases.v1.json",
    ).cases[0]
    provenance = DataProvenance(
        source_type="postgresql.products",
        source_id="products:1",
        fields=("name", "price"),
    )
    calls = tuple(
        _metadata(stage, index, fallback=(stage == "specialist.analysis"))
        for index, stage in enumerate(
            ("routing", "planning", "specialist.analysis", "synthesis"),
            start=1,
        )
    )
    result = OrchestrationResult(
        status=TaskStatus.SUCCESS,
        answer=("Tai nghe Bluetooth Nova Air S2 có giá 799.000₫ theo dữ liệu mẫu."),
        request_id="req_eval_mapping",
        trace_id="trace_eval_mapping",
        session_id="sess_eval_mapping",
        intent="product.search",
        plan=ExecutionPlan(
            plan_id="plan_eval_mapping",
            intent="product.search",
            steps=(
                ExecutionStep(
                    step_id="step_product",
                    agent_id="product_agent",
                    action="product.search",
                ),
                ExecutionStep(
                    step_id="step_review",
                    agent_id="review_agent",
                    action="review.summarize",
                    depends_on=("step_product",),
                ),
            ),
        ),
        agent_results=(
            AgentResult(
                task_id="task_eval_mapping",
                agent_id="product_agent",
                status=TaskStatus.SUCCESS,
                data={"products": [{"id": 1}]},
                provenance=(provenance,),
            ),
        ),
        provenance=(provenance,),
        model_calls=calls,
        duration_ms=100,
    )
    observed = ObservedOrchestrationTurnV2(
        result=result,
        latencies=StageLatenciesV2(
            routing_ms=10,
            planning_ms=15,
            agent_ms=50,
            synthesis_ms=20,
            end_to_end_ms=100,
        ),
    )

    observation = build_evaluation_observation_v2(
        observed=observed,
        spec=spec,
        protocol=protocol,
        variant=variant,
        pricing=pricing,
        run_id="run_eval_mapping",
        phase=EvaluationPhase.CORRECTNESS,
        repetition=0,
        execution_order=0,
    )

    assert tuple(call.stage for call in observation.model_calls) == (
        ModelStage.ROUTING,
        ModelStage.PLANNING,
        ModelStage.SPECIALIST,
        ModelStage.SYNTHESIS,
    )
    assert observation.model_calls[2].outcome == ModelCallOutcome.FALLBACK
    assert observation.model_calls[2].fallback_reason == "deterministic_specialist"
    assert observation.model_calls[0].usage is not None
    assert observation.model_calls[0].usage.cached_input_tokens == 2
    assert observation.model_calls[0].usage.reasoning_tokens == 3
    assert observation.model_calls[0].response_id == "resp_1"
    assert observation.total_tokens == 100
    assert (
        observation.estimated_cost_usd
        == estimate_observation_cost(
            observation,
            pricing,
        ).value_usd
    )
    assert observation.estimated_cost_usd == Decimal("0.000134820000")
    assert observation.dependency_edges[0].model_dump() == {
        "source_step_id": "step_product",
        "target_step_id": "step_review",
    }
    assert observation.retrieved_product_ids == (1,)
    assert observation.provenance_source_ids == ("products:1",)
    assert observation.routing_correct is True
    assert observation.assertions_passed == 3
    assert observation.exact_plan is False


def test_observation_rejects_unknown_runtime_stage() -> None:
    pricing = _pricing()
    protocol = _protocol(pricing)
    variant = protocol.variants[1]
    spec = load_evaluation_corpus_v2(
        PROJECT_ROOT / "evaluation" / "corpus.v2.json",
        PROJECT_ROOT / "evaluation" / "cases.v1.json",
    ).cases[0]
    result = OrchestrationResult(
        status=TaskStatus.SUCCESS,
        answer="Grounded answer.",
        request_id="req_eval_unknown",
        trace_id="trace_eval_unknown",
        session_id="sess_eval_unknown",
        intent="product.search",
        plan=ExecutionPlan(
            plan_id="plan_eval_unknown",
            intent="product.search",
        ),
        model_calls=(_metadata("unknown.stage", 1),),
        duration_ms=1,
    )

    with pytest.raises(ValueError, match="unknown evaluation model stage"):
        build_evaluation_observation_v2(
            observed=ObservedOrchestrationTurnV2(
                result=result,
                latencies=StageLatenciesV2(1, 0, None, 0, 1),
            ),
            spec=spec,
            protocol=protocol,
            variant=variant,
            pricing=pricing,
            run_id="run_eval_unknown",
            phase=EvaluationPhase.CORRECTNESS,
            repetition=0,
            execution_order=0,
        )


def _metadata(stage: str, index: int, *, fallback: bool = False) -> ModelCallMetadata:
    return ModelCallMetadata(
        call_id=f"mcall_{index:032x}",
        stage=stage,
        agent_id=(
            "product_agent" if stage == "specialist.analysis" else "orchestrator"
        ),
        model=(
            "gpt-5.4-mini-2026-03-17"
            if stage == "synthesis"
            else "gpt-5.4-nano-2026-03-17"
        ),
        response_id=f"resp_{index}",
        status="success",
        duration_ms=5,
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=15,
        reasoning_tokens=3,
        total_tokens=25,
        attempts=1,
        fallback_used=fallback,
        fallback_reason=("deterministic_specialist" if fallback else None),
    )


def _pricing() -> PricingManifestV2:
    return PricingManifestV2(
        pricing_id="mapping_pricing_v2",
        provider="openai",
        effective_at=date(2026, 8, 25),
        source_url="https://platform.openai.com/pricing",
        entries=(
            ModelPriceV2(
                model="gpt-5.4-nano-2026-03-17",
                input_per_million_usd=Decimal("0.20"),
                cached_input_per_million_usd=Decimal("0.02"),
                output_per_million_usd=Decimal("1.25"),
            ),
            ModelPriceV2(
                model="gpt-5.4-mini-2026-03-17",
                input_per_million_usd=Decimal("0.75"),
                cached_input_per_million_usd=Decimal("0.075"),
                output_per_million_usd=Decimal("4.50"),
            ),
        ),
    )


def _protocol(pricing: PricingManifestV2) -> EvaluationProtocolV2:
    deterministic = EvaluationVariantV2(
        variant_id="deterministic_v2",
        description="Deterministic mapping baseline.",
        runtime_mode=RuntimeMode.DETERMINISTIC,
        enabled_agents=("product_agent",),
        embedding_backend="hashed_token_cosine_v1",
    )
    hybrid = EvaluationVariantV2(
        variant_id="hybrid_mapping_v2",
        description="Hybrid mapping candidate.",
        runtime_mode=RuntimeMode.HYBRID,
        model_router_enabled=True,
        model_planner_enabled=True,
        model_specialists_enabled=True,
        model_synthesis_enabled=True,
        enabled_agents=("product_agent", "review_agent"),
        embedding_backend="hashed_token_cosine_v1",
        model_bindings=tuple(
            ModelBindingV2(
                stage=stage,
                provider="openai",
                model=(
                    "gpt-5.4-mini-2026-03-17"
                    if stage == ModelStage.SYNTHESIS
                    else "gpt-5.4-nano-2026-03-17"
                ),
            )
            for stage in (
                ModelStage.ROUTING,
                ModelStage.PLANNING,
                ModelStage.SPECIALIST,
                ModelStage.SYNTHESIS,
            )
        ),
        max_model_calls=4,
        parent_variant_id="deterministic_v2",
    )
    return EvaluationProtocolV2(
        protocol_id="mapping_protocol_v2",
        experiment_sha256="a" * 64,
        baseline_variant_id="deterministic_v2",
        corpus_id="mapping_corpus_v2",
        corpus_sha256="b" * 64,
        dataset_id="mapping_dataset_v2",
        dataset_sha256="c" * 64,
        sample_seed_sha256="d" * 64,
        evaluator_sha256="e" * 64,
        correctness_repeats=1,
        case_order=("simple_01_nova_search",),
        variants=(deterministic, hybrid),
        model_runtime_policy=ModelRuntimePolicyV2(
            request_timeout_seconds=18,
            max_retries=2,
            max_output_tokens=1_200,
            max_concurrency=8,
            circuit_failure_threshold=4,
            circuit_recovery_seconds=30,
        ),
        pricing_sha256=canonical_sha256(pricing),
        git_revision="abcdef1",
        git_dirty=False,
        network_allowed=True,
    )
