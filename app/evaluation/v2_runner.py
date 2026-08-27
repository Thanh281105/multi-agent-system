"""Command-line entry point for frozen paired evaluation v2."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.evaluation.artifacts import validate_bundle
from app.evaluation.comparison import compare_variants
from app.evaluation.execution import run_evaluation_matrix_v2
from app.evaluation.experiment import load_evaluation_experiment_v2
from app.evaluation.preparation import build_evaluation_protocol_v2
from app.evaluation.reporting import write_evaluation_bundle_v2
from app.evaluation.v2_models import (
    ComparisonMetric,
    EvaluationObservationV2,
    EvaluationPhase,
    EvaluationProtocolV2,
    EvaluationVariantV2,
    ModelRuntimePolicyV2,
)
from app.shared import ModelRuntime, OpenAIModelRuntime


def cli(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            return _run_command(arguments)
        if arguments.command == "validate":
            return _validate_command(arguments)
        if arguments.command == "compare":
            return _compare_command(arguments)
        raise AssertionError(f"unhandled command: {arguments.command}")
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc)[:500]})
        return 2


def main() -> None:
    raise SystemExit(cli())


def _run_command(arguments: argparse.Namespace) -> int:
    assets = load_evaluation_experiment_v2(
        arguments.experiment,
        arguments.corpus,
        arguments.cases,
        arguments.pricing,
    )
    protocol = build_evaluation_protocol_v2(
        assets,
        project_root=arguments.project_root,
        variant_ids=(tuple(arguments.variant) if arguments.variant else None),
        max_cases=arguments.max_cases,
        allow_dirty=arguments.allow_dirty,
    )
    runtime_factory = None
    if protocol.network_allowed:
        if not arguments.allow_network:
            raise RuntimeError(
                "hybrid evaluation requires explicit --allow-network consent"
            )
        policy = protocol.model_runtime_policy
        if policy is None:
            raise RuntimeError("hybrid evaluation protocol omits runtime policy")
        runtime_factory = _openai_runtime_factory(
            Settings().openai_api_key_value,
            policy,
        )
    run_id = arguments.run_id or _new_run_id()
    output = arguments.output or (
        arguments.project_root / "output" / "evaluation-v2" / run_id
    )
    execution = asyncio.run(
        run_evaluation_matrix_v2(
            run_id=run_id,
            assets=assets,
            protocol=protocol,
            runtime_factory=runtime_factory,
        )
    )
    completed = write_evaluation_bundle_v2(
        output,
        run_id=run_id,
        execution=execution,
        pricing=assets.pricing,
    )
    _emit(
        {
            "status": "complete",
            "run_id": run_id,
            "output": str(output.resolve()),
            "observations": completed.manifest.observation_count,
            "comparisons": completed.manifest.comparison_count,
            "omissions": completed.manifest.omission_count,
            "git_dirty": completed.manifest.git_dirty,
        }
    )
    return 0


def _validate_command(arguments: argparse.Namespace) -> int:
    manifest = validate_bundle(arguments.bundle)
    _emit(
        {
            "status": "valid",
            "run_id": manifest.run_id,
            "observations": manifest.observation_count,
            "comparisons": manifest.comparison_count,
            "omissions": manifest.omission_count,
            "completion_status": manifest.completion_status,
            "protocol_sha256": manifest.protocol_sha256,
        }
    )
    return 0


def _compare_command(arguments: argparse.Namespace) -> int:
    validate_bundle(arguments.bundle)
    protocol = EvaluationProtocolV2.model_validate_json(
        (arguments.bundle / "protocol.json").read_text(encoding="utf-8")
    )
    observations = tuple(
        EvaluationObservationV2.model_validate_json(line)
        for line in (arguments.bundle / "observations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    )
    comparison = compare_variants(
        observations,
        baseline_variant_id=(arguments.baseline or protocol.baseline_variant_id),
        candidate_variant_id=arguments.candidate,
        metric=ComparisonMetric(arguments.metric),
        phase=EvaluationPhase(arguments.phase),
        bootstrap_samples=(
            arguments.bootstrap_samples
            if arguments.bootstrap_samples is not None
            else protocol.bootstrap_samples
        ),
        random_seed=protocol.random_seed,
    )
    _emit(comparison.model_dump(mode="json"))
    return 0


def _openai_runtime_factory(
    api_key: str,
    policy: ModelRuntimePolicyV2,
) -> Callable[[EvaluationVariantV2], ModelRuntime]:
    if not api_key:
        raise RuntimeError("hybrid evaluation requires OPENAI_API_KEY")

    def create(_: EvaluationVariantV2) -> ModelRuntime:
        return OpenAIModelRuntime(
            api_key,
            timeout_seconds=policy.request_timeout_seconds,
            max_retries=policy.max_retries,
            max_output_tokens=policy.max_output_tokens,
            max_concurrency=policy.max_concurrency,
            circuit_failure_threshold=policy.circuit_failure_threshold,
            circuit_recovery_seconds=policy.circuit_recovery_seconds,
        )

    return create


def _parser() -> argparse.ArgumentParser:
    project_root = _default_project_root()
    parser = argparse.ArgumentParser(
        description="Run and verify the frozen paired multi-agent evaluation v2.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="execute and publish an atomic bundle")
    run.add_argument("--project-root", type=Path, default=project_root)
    run.add_argument(
        "--experiment",
        type=Path,
        default=project_root / "evaluation" / "experiment.v2.json",
    )
    run.add_argument(
        "--corpus",
        type=Path,
        default=project_root / "evaluation" / "corpus.v2.json",
    )
    run.add_argument(
        "--cases",
        type=Path,
        default=project_root / "evaluation" / "cases.v1.json",
    )
    run.add_argument(
        "--pricing",
        type=Path,
        default=(
            project_root
            / "evaluation"
            / "pricing"
            / "openai-standard-2026-08-25.v2.json"
        ),
    )
    run.add_argument("--output", type=Path)
    run.add_argument("--run-id")
    run.add_argument("--variant", action="append")
    run.add_argument("--max-cases", type=int)
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument("--allow-network", action="store_true")

    validate = subparsers.add_parser("validate", help="verify every bundle hash")
    validate.add_argument("--bundle", type=Path, required=True)

    compare = subparsers.add_parser(
        "compare",
        help="recompute one paired comparison from a verified bundle",
    )
    compare.add_argument("--bundle", type=Path, required=True)
    compare.add_argument("--baseline")
    compare.add_argument("--candidate", required=True)
    compare.add_argument(
        "--metric",
        choices=[metric.value for metric in ComparisonMetric],
        required=True,
    )
    compare.add_argument(
        "--phase",
        choices=[phase.value for phase in EvaluationPhase],
        required=True,
    )
    compare.add_argument("--bootstrap-samples", type=int)
    return parser


def _default_project_root() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "evaluation" / "experiment.v2.json").is_file():
            return candidate
    return Path.cwd()


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz").lower()
    return f"run_{timestamp}_{uuid4().hex[:8]}"


def _emit(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
