"""Complete evaluation v2 matrix execution tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation.execution import run_evaluation_matrix_v2
from app.evaluation.experiment import load_evaluation_experiment_v2
from app.evaluation.preparation import GitStateV2, build_evaluation_protocol_v2
from app.evaluation.protocol import validate_observation_protocol

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


def _assets():
    return load_evaluation_experiment_v2(
        PROJECT_ROOT / "evaluation" / "experiment.v2.json",
        PROJECT_ROOT / "evaluation" / "corpus.v2.json",
        PROJECT_ROOT / "evaluation" / "cases.v1.json",
        PROJECT_ROOT / "evaluation" / "pricing" / "openai-standard-2026-08-25.v2.json",
    )
