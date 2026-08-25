"""Complete evaluation v2 matrix execution tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.reasoning import SpecialistInsight
from app.evaluation.artifacts import validate_bundle
from app.evaluation.execution import run_evaluation_matrix_v2
from app.evaluation.experiment import load_evaluation_experiment_v2
from app.evaluation.preparation import GitStateV2, build_evaluation_protocol_v2
from app.evaluation.protocol import validate_observation_protocol
from app.evaluation.reporting import write_evaluation_bundle_v2
from app.evaluation.v2_models import EvaluationPhase, ModelStage
from app.orchestrator.model_schemas import (
    GroundedClaim,
    GroundedSynthesis,
    PlanningDecision,
    RoutingDecision,
    RoutingEntities,
)
from app.shared import OpenAIModelRuntime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_deterministic_matrix_runs_without_key_or_runtime_factory() -> None:
    assets = _assets()
    prepared = build_evaluation_protocol_v2(
        assets,
        project_root=PROJECT_ROOT,
        variant_ids=("deterministic_v2",),
        max_cases=1,
        git_state=GitStateV2(revision="abcdef1", dirty=False),
    )
    protocol = prepared.model_copy(
        update={
            "correctness_repeats": 1,
            "warmup_repeats": 0,
            "warmup_case_id": None,
            "latency_repeats": 0,
            "latency_case_order": (),
        }
    )

    execution = await run_evaluation_matrix_v2(
        run_id="run_deterministic_v2",
        assets=assets,
        protocol=protocol,
    )

    assert execution.sample_counts == {
        "shops": 5,
        "products": 30,
        "reviews": 150,
    }
    assert len(execution.observations) == 1
    observation = execution.observations[0]
    validate_observation_protocol(observation, protocol)
    assert observation.variant_id == "deterministic_v2"
    assert observation.model_calls == ()
    assert observation.total_tokens == 0
    assert observation.estimated_cost_usd == Decimal("0E-12")
    assert observation.task_success is True
    assert observation.routing_correct is True
    assert observation.exact_plan is True


@pytest.mark.asyncio
async def test_matrix_rejects_hybrid_without_runtime_before_execution() -> None:
    assets = _assets()
    protocol = build_evaluation_protocol_v2(
        assets,
        project_root=PROJECT_ROOT,
        variant_ids=("hybrid_full",),
        max_cases=1,
        git_state=GitStateV2(revision="abcdef1", dirty=False),
    )

    with pytest.raises(RuntimeError, match="runtime factory"):
        await run_evaluation_matrix_v2(
            run_id="run_missing_runtime_v2",
            assets=assets,
            protocol=protocol,
        )


@pytest.mark.asyncio
async def test_hybrid_matrix_runs_bound_models_and_discards_warmup_calls(
    tmp_path: Path,
) -> None:
    assets = _assets()
    prepared = build_evaluation_protocol_v2(
        assets,
        project_root=PROJECT_ROOT,
        variant_ids=("hybrid_full",),
        max_cases=1,
        git_state=GitStateV2(revision="abcdef1", dirty=False),
    )
    protocol = prepared.model_copy(
        update={
            "correctness_repeats": 1,
            "warmup_repeats": 1,
            "latency_repeats": 1,
        }
    )
    responses = _SchemaAwareResponses()

    execution = await run_evaluation_matrix_v2(
        run_id="run_hybrid_matrix_v2",
        assets=assets,
        protocol=protocol,
        runtime_factory=lambda _: OpenAIModelRuntime(
            "test-key",
            client=SimpleNamespace(responses=responses),
            max_retries=0,
        ),
    )

    assert len(execution.observations) == 4
    hybrid = [
        item for item in execution.observations if item.variant_id == "hybrid_full"
    ]
    deterministic = [
        item for item in execution.observations if item.variant_id == "deterministic_v2"
    ]
    assert {item.phase for item in hybrid} == {
        EvaluationPhase.CORRECTNESS,
        EvaluationPhase.LATENCY,
    }
    assert all(item.model_calls == () for item in deterministic)
    assert all(
        tuple(call.stage for call in item.model_calls)
        == (
            ModelStage.ROUTING,
            ModelStage.PLANNING,
            ModelStage.SPECIALIST,
            ModelStage.SYNTHESIS,
        )
        for item in hybrid
    )
    assert all(item.task_success for item in hybrid)
    assert all(item.estimated_cost_usd is not None for item in hybrid)
    assert all((item.estimated_cost_usd or Decimal(0)) > 0 for item in hybrid)
    assert all(
        call.response_id is not None
        and call.usage is not None
        and call.usage.cached_input_tokens == 4
        and call.usage.reasoning_tokens == 3
        for item in hybrid
        for call in item.model_calls
    )
    assert len(responses.requests) == 12
    assert sum(len(item.model_calls) for item in hybrid) == 8
    assert all(request["store"] is False for request in responses.requests)
    assert {
        request["model"]
        for request in responses.requests
        if request["metadata"]["stage"] != "synthesis"
    } == {"gpt-5.4-nano-2026-03-17"}
    assert {
        request["model"]
        for request in responses.requests
        if request["metadata"]["stage"] == "synthesis"
    } == {"gpt-5.4-mini-2026-03-17"}
    assert all(
        request["reasoning"] == {"effort": "low"} for request in responses.requests
    )
    for observation in execution.observations:
        validate_observation_protocol(observation, protocol)
    completed = write_evaluation_bundle_v2(
        tmp_path / "hybrid_bundle",
        run_id="run_hybrid_matrix_v2",
        execution=execution,
        pricing=assets.pricing,
    )
    assert completed.manifest.comparison_count == 8
    assert completed.manifest.omission_count == 0
    assert completed.analysis.robustness == ()
    assert validate_bundle(tmp_path / "hybrid_bundle") == completed.manifest


class _SchemaAwareResponses:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def parse(self, **request: Any) -> Any:
        self.requests.append(request)
        schema = request["text_format"]
        if schema is RoutingDecision:
            value: Any = RoutingDecision(
                intent="product.search",
                confidence=0.99,
                entities=RoutingEntities(product_query="Nova Air S2"),
                rationale="exact_product_lookup",
            )
        elif schema is PlanningDecision:
            value = PlanningDecision(
                capabilities=["product.search"],
                candidate_limit=1,
                rationale="single_product_search",
            )
        elif schema is SpecialistInsight:
            value = SpecialistInsight(
                summary="Nova Air S2 được tìm thấy trong dữ liệu mẫu.",
                findings=["Giá và tên đã được tool xác nhận."],
                caveats=["Chỉ áp dụng cho dữ liệu mẫu."],
                evidence_source_ids=["postgresql:products"],
                confidence=0.95,
            )
        elif schema is GroundedSynthesis:
            value = GroundedSynthesis(
                claims=[
                    GroundedClaim(
                        statement=(
                            "Tai nghe Bluetooth Nova Air S2 có giá 799.000₫ "
                            "trong dữ liệu mẫu."
                        ),
                        source_ids=["postgresql:products"],
                    )
                ],
            )
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema: {schema}")
        index = len(self.requests)
        return SimpleNamespace(
            id=f"resp_eval_{index}",
            status="completed",
            output_parsed=value,
            usage=SimpleNamespace(
                input_tokens=20,
                input_tokens_details=SimpleNamespace(cached_tokens=4),
                output_tokens=10,
                output_tokens_details=SimpleNamespace(reasoning_tokens=3),
                total_tokens=30,
            ),
        )


def _assets():
    return load_evaluation_experiment_v2(
        PROJECT_ROOT / "evaluation" / "experiment.v2.json",
        PROJECT_ROOT / "evaluation" / "corpus.v2.json",
        PROJECT_ROOT / "evaluation" / "cases.v1.json",
        PROJECT_ROOT / "evaluation" / "pricing" / "openai-standard-2026-08-25.v2.json",
    )
