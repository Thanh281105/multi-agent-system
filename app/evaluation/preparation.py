"""Prepare a source-bound evaluation v2 protocol before any model call."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.evaluation.experiment import LoadedEvaluationExperimentV2
from app.evaluation.v2_models import (
    EvaluationProtocolV2,
    EvaluationVariantV2,
    RuntimeMode,
)


@dataclass(frozen=True, slots=True)
class GitStateV2:
    revision: str
    dirty: bool


def read_git_state_v2(project_root: Path) -> GitStateV2:
    revision = _run_git(project_root, "rev-parse", "--verify", "HEAD").strip()
    if not revision or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise RuntimeError("evaluation requires a valid Git HEAD revision")
    status = _run_git(
        project_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    )
    return GitStateV2(revision=revision, dirty=bool(status.strip()))


def source_manifest_sha256_v2(project_root: Path) -> str:
    source_root = project_root / "app"
    source_paths = sorted(
        source_root.rglob("*.py"),
        key=lambda path: path.relative_to(project_root).as_posix(),
    )
    if not source_paths:
        raise RuntimeError("evaluation source manifest found no application files")
    digest = hashlib.sha256()
    for source_path in source_paths:
        relative_path = source_path.relative_to(project_root).as_posix()
        content_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        digest.update(f"{relative_path}\0{content_hash}\n".encode("utf-8"))
    return digest.hexdigest()


def build_evaluation_protocol_v2(
    assets: LoadedEvaluationExperimentV2,
    *,
    project_root: Path,
    variant_ids: tuple[str, ...] | None = None,
    max_cases: int | None = None,
    git_state: GitStateV2 | None = None,
    allow_dirty: bool = False,
) -> EvaluationProtocolV2:
    state = git_state or read_git_state_v2(project_root)
    if state.dirty and not allow_dirty:
        raise RuntimeError(
            "refusing to evaluate a dirty worktree; commit changes or use allow_dirty"
        )
    selected_variants = _select_variants(assets, variant_ids)
    available_cases = assets.corpus.cases
    if max_cases is not None and not 1 <= max_cases <= len(available_cases):
        raise ValueError("max_cases must select between one and the corpus size")
    selected_cases = available_cases[:max_cases] if max_cases else available_cases
    case_order = tuple(case.case_id for case in selected_cases)
    case_ids = set(case_order)
    config = assets.config
    if config.warmup_case_id is not None and config.warmup_case_id not in case_ids:
        raise ValueError("selected case slice excludes the pinned warmup case")
    latency_case_order = tuple(
        case_id for case_id in config.latency_case_ids if case_id in case_ids
    )
    if config.latency_repeats and not latency_case_order:
        raise ValueError("selected case slice contains no pinned latency case")
    return EvaluationProtocolV2(
        protocol_id=config.experiment_id,
        experiment_sha256=assets.experiment_sha256,
        baseline_variant_id=config.baseline_variant_id,
        corpus_id=assets.corpus.manifest.corpus_id,
        corpus_sha256=assets.corpus.corpus_sha256,
        dataset_id=assets.corpus.manifest.base_dataset_id,
        dataset_sha256=assets.corpus.dataset_sha256,
        evaluator_sha256=source_manifest_sha256_v2(project_root),
        random_seed=config.random_seed,
        correctness_repeats=config.correctness_repeats,
        warmup_repeats=config.warmup_repeats,
        latency_repeats=config.latency_repeats,
        bootstrap_samples=config.bootstrap_samples,
        case_order=case_order,
        latency_case_order=latency_case_order,
        warmup_case_id=config.warmup_case_id,
        variants=selected_variants,
        pricing_sha256=assets.pricing_sha256,
        git_revision=state.revision,
        git_dirty=state.dirty,
        network_allowed=any(
            variant.runtime_mode == RuntimeMode.HYBRID for variant in selected_variants
        ),
    )


def _select_variants(
    assets: LoadedEvaluationExperimentV2,
    requested_variant_ids: tuple[str, ...] | None,
) -> tuple[EvaluationVariantV2, ...]:
    configured = {variant.variant_id: variant for variant in assets.config.variants}
    requested = set(requested_variant_ids or configured)
    unknown = requested - set(configured)
    if unknown:
        raise ValueError(f"unknown evaluation variants: {sorted(unknown)}")
    requested.add(assets.config.baseline_variant_id)
    pending = list(requested)
    while pending:
        variant = configured[pending.pop()]
        if (
            variant.parent_variant_id is not None
            and variant.parent_variant_id not in requested
        ):
            requested.add(variant.parent_variant_id)
            pending.append(variant.parent_variant_id)
    return tuple(
        variant for variant in assets.config.variants if variant.variant_id in requested
    )


def _run_git(project_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(project_root), *arguments),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("evaluation could not inspect Git state") from exc
    return completed.stdout
