"""Focused local-only operator boundary tests for Package 8."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.evaluation import benchmark_cli


class _Preparation(BaseModel):
    preparation_sha256: str
    schedule_sha256: str


class _CalibrationInputs(BaseModel):
    inputs_sha256: str


def _artifact_pair(*, marker: str) -> tuple[_Preparation, _CalibrationInputs]:
    return (
        _Preparation(preparation_sha256=marker * 64, schedule_sha256="b" * 64),
        _CalibrationInputs(inputs_sha256="c" * 64),
    )


def test_preparation_artifacts_are_immutable_and_detect_drift(tmp_path: Path) -> None:
    directory = tmp_path / "p8-preparation"
    preparation, calibration = _artifact_pair(marker="a")

    benchmark_cli._write_or_validate_preparation_artifacts(
        directory,
        preparation=preparation,  # type: ignore[arg-type]
        calibration_inputs=calibration,  # type: ignore[arg-type]
    )
    benchmark_cli._write_or_validate_preparation_artifacts(
        directory,
        preparation=preparation,  # type: ignore[arg-type]
        calibration_inputs=calibration,  # type: ignore[arg-type]
    )

    changed, _ = _artifact_pair(marker="d")
    with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
        benchmark_cli._write_or_validate_preparation_artifacts(
            directory,
            preparation=changed,  # type: ignore[arg-type]
            calibration_inputs=calibration,  # type: ignore[arg-type]
        )
    assert caught.value.code == "preparation_artifact_drift"


def test_completed_heldout_checkpoint_is_opened_only_with_forbidden_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Runner:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        async def run(self) -> object:
            return SimpleNamespace(
                summary=SimpleNamespace(
                    total_scheduled=1,
                    completed_turn_ids=("turn_1",),
                    failed_turn_ids=(),
                    ambiguous_turn_ids=(),
                    orphan_started_turn_ids=(),
                    missing_turn_ids=(),
                ),
                receipts=("receipt",),
            )

    monkeypatch.setattr(benchmark_cli, "HeldoutEvaluationV3ObservationRunner", _Runner)

    receipts = benchmark_cli._load_complete_heldout_receipts(
        frozen=SimpleNamespace(),  # type: ignore[arg-type]
        schedule=(SimpleNamespace(),),  # type: ignore[arg-type]
        checkpoint=tmp_path / "already-complete.jsonl",
    )

    assert receipts == ("receipt",)
    assert isinstance(observed["executor_factory"], benchmark_cli._ForbiddenFactory)


def test_local_prepare_rejects_citations_without_exact_source_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(citations=(SimpleNamespace(),))
    durable = SimpleNamespace(result=result)
    monkeypatch.setattr(
        benchmark_cli.DurableTurnOutcome,
        "model_validate",
        lambda _: durable,
    )
    receipt = SimpleNamespace(
        turn_results=(SimpleNamespace(result_payload={"status": "completed"}),)
    )

    with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
        benchmark_cli._reject_unresolved_local_citations((receipt,))  # type: ignore[arg-type]
    assert caught.value.code == "citation_authority_required"


def test_run_requires_network_consent_before_live_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _unexpected_factory(**_: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("live factory must not be reached without consent")

    monkeypatch.setattr(
        benchmark_cli, "_build_package7_live_executor_factory", _unexpected_factory
    )
    frozen = SimpleNamespace(protocol_sha256="a" * 64, protocol=object())
    schedule = (SimpleNamespace(),)
    monkeypatch.setattr(benchmark_cli, "_load_frozen", lambda _: frozen)
    monkeypatch.setattr(
        benchmark_cli,
        "build_heldout_schedule_v3",
        lambda *_args, **_kwargs: schedule,
    )
    monkeypatch.setattr(
        benchmark_cli,
        "build_heldout_execution_plan_v3",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    assert (
        benchmark_cli.cli(
            (
                "run",
                "--project-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "heldout"),
            )
        )
        == 2
    )
    assert not called


def test_operate_requires_network_consent_before_loading_any_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = False

    def _unexpected_load(_: object) -> object:
        nonlocal loaded
        loaded = True
        raise AssertionError("frozen inputs must not be loaded before network consent")

    monkeypatch.setattr(benchmark_cli, "_load_frozen", _unexpected_load)

    assert (
        benchmark_cli.cli(
            (
                "operate",
                "--project-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "heldout"),
                "--database-url",
                "postgresql://operator:placeholder@localhost/evaluation",
                "--judge-budget-nano-usd",
                "1",
            )
        )
        == 2
    )
    assert not loaded


def test_exact_resolver_refuses_missing_authority_without_service_call() -> None:
    resolver = benchmark_cli._ReceiptExactEvidenceResolver(
        knowledge_service=object(),
        corpus_version_id="cor_" + "a" * 60,
        index_manifest_id="idx_" + "b" * 60,
        authorities={},
    )
    binding = benchmark_cli.EvidenceBindingKeyV3(
        evidence_id="evidence_missing",
        source_id="source_missing",
        source_version_id="version_missing",
        chunk_id="chunk_missing",
        span_id="span_missing",
    )

    with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
        resolver.resolve(binding)
    assert caught.value.code == "exact_evidence_authority_unavailable"
