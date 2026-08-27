"""Evaluation v2 CLI safety and bundle boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.evaluation.artifacts import write_bundle
from app.evaluation.protocol import protocol_sha256
from app.evaluation.v2_models import (
    EvaluationObservationV2,
    EvaluationProtocolV2,
    EvaluationVariantV2,
    ModelRuntimePolicyV2,
    PricingManifestV2,
    RuntimeMode,
)
from app.evaluation.v2_runner import _openai_runtime_factory, cli
from app.shared import OpenAIModelRuntime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_factory_uses_only_protocol_bound_controls() -> None:
    policy = ModelRuntimePolicyV2(
        request_timeout_seconds=7,
        max_retries=1,
        max_output_tokens=777,
        max_concurrency=3,
        circuit_failure_threshold=2,
        circuit_recovery_seconds=9,
    )
    variant = EvaluationVariantV2(
        variant_id="deterministic_factory_v2",
        description="Factory construction peer.",
        runtime_mode=RuntimeMode.DETERMINISTIC,
        enabled_agents=("product_agent",),
        embedding_backend="hashed_token_cosine_v1",
    )

    runtime = _openai_runtime_factory("test-key", policy)(variant)

    assert isinstance(runtime, OpenAIModelRuntime)
    assert runtime._timeout_seconds == 7
    assert runtime._max_retries == 1
    assert runtime._max_output_tokens == 777
    assert runtime._semaphore._value == 3
    assert runtime._circuit_failure_threshold == 2
    assert runtime._circuit_recovery_seconds == 9


def test_cli_runs_deterministic_without_key_then_validates_bundle(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    output = tmp_path / "deterministic_bundle"

    exit_code = cli(
        (
            "run",
            "--project-root",
            str(PROJECT_ROOT),
            "--variant",
            "deterministic_v2",
            "--max-cases",
            "1",
            "--allow-dirty",
            "--run-id",
            "run_cli_deterministic_v2",
            "--output",
            str(output),
        )
    )
    run_payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert run_payload["status"] == "complete"
    assert run_payload["observations"] == 8
    assert run_payload["comparisons"] == 0
    assert run_payload["omissions"] == 0
    assert output.is_dir()

    assert cli(("validate", "--bundle", str(output))) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["status"] == "valid"
    assert validation["run_id"] == "run_cli_deterministic_v2"
    assert validation["completion_status"] == "complete"


def test_cli_recomputes_comparison_only_after_bundle_validation(
    tmp_path: Path,
    capsys,
) -> None:
    source_bundle = tmp_path / "source_bundle"
    assert (
        cli(
            (
                "run",
                "--project-root",
                str(PROJECT_ROOT),
                "--variant",
                "deterministic_v2",
                "--max-cases",
                "1",
                "--allow-dirty",
                "--run-id",
                "run_cli_source_v2",
                "--output",
                str(source_bundle),
            )
        )
        == 0
    )
    capsys.readouterr()
    source_protocol = EvaluationProtocolV2.model_validate_json(
        (source_bundle / "protocol.json").read_text(encoding="utf-8")
    )
    baseline = source_protocol.variants[0]
    candidate = baseline.model_copy(
        update={
            "variant_id": "candidate_v2",
            "description": "Deterministic CLI comparison peer.",
            "parent_variant_id": baseline.variant_id,
        }
    )
    protocol = EvaluationProtocolV2.model_validate(
        {
            **source_protocol.model_dump(),
            "variants": [baseline.model_dump(), candidate.model_dump()],
        }
    )
    source_observations = tuple(
        EvaluationObservationV2.model_validate_json(line)
        for line in (source_bundle / "observations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    )
    paired_observations: list[EvaluationObservationV2] = []
    for observation in source_observations:
        common = {
            "run_id": "run_cli_compare_v2",
            "protocol_sha256": protocol_sha256(protocol),
        }
        paired_observations.append(
            observation.model_copy(
                update={**common, "execution_order": len(paired_observations)}
            )
        )
        paired_observations.append(
            observation.model_copy(
                update={
                    **common,
                    "variant_id": candidate.variant_id,
                    "execution_order": len(paired_observations),
                }
            )
        )
    pricing = PricingManifestV2.model_validate_json(
        (source_bundle / "pricing.json").read_text(encoding="utf-8")
    )
    comparison_bundle = tmp_path / "comparison_bundle"
    write_bundle(
        comparison_bundle,
        run_id="run_cli_compare_v2",
        protocol=protocol,
        observations=paired_observations,
        comparisons=(),
        pricing=pricing,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert (
        cli(
            (
                "compare",
                "--bundle",
                str(comparison_bundle),
                "--candidate",
                "candidate_v2",
                "--metric",
                "task_success",
                "--phase",
                "correctness",
                "--bootstrap-samples",
                "100",
            )
        )
        == 0
    )
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["baseline_variant_id"] == "deterministic_v2"
    assert comparison["candidate_variant_id"] == "candidate_v2"
    assert comparison["metric"] == "task_success"


def test_cli_requires_explicit_network_consent_before_hybrid_execution(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "must_not_exist"

    exit_code = cli(
        (
            "run",
            "--project-root",
            str(PROJECT_ROOT),
            "--variant",
            "hybrid_full",
            "--max-cases",
            "1",
            "--allow-dirty",
            "--run-id",
            "run_cli_hybrid_v2",
            "--output",
            str(output),
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload == {
        "status": "error",
        "error": "hybrid evaluation requires explicit --allow-network consent",
    }
    assert not output.exists()
