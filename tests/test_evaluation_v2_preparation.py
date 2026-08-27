"""Source and protocol preparation tests for evaluation v2."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evaluation.experiment import load_evaluation_experiment_v2
from app.evaluation.preparation import (
    GitStateV2,
    build_evaluation_protocol_v2,
    source_manifest_sha256_v2,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_protocol_builder_closes_variant_parents_and_binds_sources() -> None:
    assets = _assets()

    protocol = build_evaluation_protocol_v2(
        assets,
        project_root=PROJECT_ROOT,
        variant_ids=("hybrid_no_router",),
        max_cases=1,
        git_state=GitStateV2(revision="abcdef1", dirty=False),
    )

    assert tuple(variant.variant_id for variant in protocol.variants) == (
        "deterministic_v2",
        "hybrid_full",
        "hybrid_no_router",
    )
    assert protocol.case_order == ("simple_01_nova_search",)
    assert protocol.latency_case_order == ("simple_01_nova_search",)
    assert protocol.warmup_case_id == "simple_01_nova_search"
    assert protocol.experiment_sha256 == assets.experiment_sha256
    assert protocol.pricing_sha256 == assets.pricing_sha256
    assert protocol.network_allowed is True
    assert protocol.model_runtime_policy == assets.config.model_runtime_policy
    assert "api_key" not in protocol.model_dump_json()
    assert len(protocol.sample_seed_sha256) == 64
    assert len(protocol.evaluator_sha256) == 64


def test_protocol_builder_rejects_dirty_or_unknown_inputs() -> None:
    assets = _assets()
    dirty = GitStateV2(revision="abcdef1", dirty=True)

    with pytest.raises(RuntimeError, match="dirty worktree"):
        build_evaluation_protocol_v2(
            assets,
            project_root=PROJECT_ROOT,
            max_cases=1,
            git_state=dirty,
        )
    allowed = build_evaluation_protocol_v2(
        assets,
        project_root=PROJECT_ROOT,
        variant_ids=("deterministic_v2",),
        max_cases=1,
        git_state=dirty,
        allow_dirty=True,
    )
    assert allowed.git_dirty is True
    assert allowed.network_allowed is False
    assert allowed.model_runtime_policy is None
    with pytest.raises(ValueError, match="unknown evaluation variants"):
        build_evaluation_protocol_v2(
            assets,
            project_root=PROJECT_ROOT,
            variant_ids=("unknown_variant",),
            max_cases=1,
            git_state=GitStateV2(revision="abcdef1", dirty=False),
        )


def test_source_manifest_hash_changes_with_source_content(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    module = source / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    before = source_manifest_sha256_v2(tmp_path)

    module.write_text("VALUE = 2\n", encoding="utf-8")

    assert source_manifest_sha256_v2(tmp_path) != before


def test_source_manifest_hash_is_line_ending_stable(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    module = source / "module.py"
    module.write_bytes(b"VALUE = 1\n")
    lf_hash = source_manifest_sha256_v2(tmp_path)

    module.write_bytes(b"VALUE = 1\r\n")

    assert source_manifest_sha256_v2(tmp_path) == lf_hash


def _assets():
    return load_evaluation_experiment_v2(
        PROJECT_ROOT / "evaluation" / "experiment.v2.json",
        PROJECT_ROOT / "evaluation" / "corpus.v2.json",
        PROJECT_ROOT / "evaluation" / "cases.v1.json",
        PROJECT_ROOT / "evaluation" / "pricing" / "openai-standard-2026-08-25.v2.json",
    )
