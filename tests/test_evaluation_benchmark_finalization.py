"""Focused accounting and publication tests for additive Package 8 finalization."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from app.evaluation.benchmark_finalization import (
    Package8FinalizationErrorV3,
    Package8JudgingPreparationV3,
    _account_judge_jobs,
    _write_report_manifest,
    build_package8_judge_bindings_v3,
    package8_postprocessing_source_sha256_v3,
)
from app.evaluation.benchmark_judging import (
    FrozenJudgeRunError,
    JudgeJobIdentityV3,
    JudgeJobResultV3,
    JudgePhaseV3,
    JudgeRecordV3,
    build_development_judge_run_key_v3,
    build_heldout_judge_run_key_v3,
)
from app.evaluation.benchmark_v3 import SuccessorBindingV3
from app.evaluation.protocol import canonical_sha256
from tests.test_evaluation_benchmark_judge_successor import _successor_inputs
from tests.test_evaluation_v3_claim_calibration import _freeze, _inputs
from tests.test_evaluation_v3_judge import _packet


def _binding_inputs(monkeypatch: pytest.MonkeyPatch, *, successor: bool = True):
    if successor:
        row, references, calibration, binding = _successor_inputs()
    else:
        from app.evaluation.v3_judge import build_calibration_reference_bundle_v3

        row = _inputs((False,), (False,))
        references = build_calibration_reference_bundle_v3(
            protocol_sha256=row[0].bindings.protocol_sha256, references=(row[2],)
        )
        calibration, binding = _freeze((row,)), None
    configuration, case, _, thresholds, _ = row
    packet = _packet(configuration.bindings, (case.answer,))
    # Preparation shape/blinding is tested separately. Keep real configuration,
    # reference, calibration, packet and run-key guards at this failing boundary.
    preparation = SimpleNamespace(
        model_dump=lambda **_: {},
        preparation_sha256="a" * 64,
        protocol_sha256=configuration.bindings.protocol_sha256,
        postprocessing_source_manifest_sha256=package8_postprocessing_source_sha256_v3(),
        blinded=SimpleNamespace(packet=packet),
    )
    monkeypatch.setattr(
        Package8JudgingPreparationV3, "model_validate", lambda _: preparation
    )
    arguments = dict(
        preparation=preparation,
        configuration=configuration,
        references=references,
        thresholds=thresholds,
        calibration=calibration,
    )
    return arguments, binding


def test_successor_finalization_bindings_match_the_durable_runner_keys(monkeypatch):
    arguments, binding = _binding_inputs(monkeypatch)
    result = build_package8_judge_bindings_v3(**arguments, successor_binding=binding)
    common = dict(
        configuration=arguments["configuration"],
        additive_source_manifest_sha256=package8_postprocessing_source_sha256_v3(),
        successor_binding=binding,
    )
    development = build_development_judge_run_key_v3(
        references=arguments["references"], thresholds=arguments["thresholds"], **common
    )
    heldout = build_heldout_judge_run_key_v3(
        packet=arguments["preparation"].blinded.packet,
        calibration=arguments["calibration"],
        **common,
    )
    assert result.development_judge_run_key_sha256 == development.run_key_sha256
    assert result.heldout_judge_run_key_sha256 == heldout.run_key_sha256


def test_finalization_still_requires_explicit_successor_authority(monkeypatch):
    arguments, _ = _binding_inputs(monkeypatch)
    with pytest.raises(FrozenJudgeRunError, match="package7_protocol_hash_drift"):
        build_package8_judge_bindings_v3(**arguments)


@pytest.mark.parametrize(
    "field",
    ["protocol_sha256", "repeat_decision_sha256", "gold_sha256", "split_sha256"],
)
def test_finalization_rejects_rehashed_foreign_successor_pins(monkeypatch, field):
    arguments, binding = _binding_inputs(monkeypatch)
    payload = binding.model_dump(mode="json", exclude={"binding_sha256"})
    payload[field] = "e" * 64
    wrong = SuccessorBindingV3(**payload, binding_sha256=canonical_sha256(payload))
    with pytest.raises(FrozenJudgeRunError, match="successor_judge_bindings_mismatch"):
        build_package8_judge_bindings_v3(**arguments, successor_binding=wrong)


def test_historical_finalization_keeps_default_authority(monkeypatch):
    arguments, binding = _binding_inputs(monkeypatch, successor=False)
    assert binding is None
    result = build_package8_judge_bindings_v3(**arguments)
    assert (
        result.judge_configuration_sha256
        == arguments["configuration"].configuration_sha256
    )


def test_judge_accounting_includes_known_unknown_and_retry_costs() -> None:
    development = _job(
        phase="development_calibration",
        target_id="calibration_dev_001",
        scope_id="judge_scope_dev",
        attempts=(
            _attempt(
                attempt_id="attempt_dev_1",
                scope_id="judge_scope_dev",
                call_id="call_dev",
                usage_status="known",
                actual_cost_nano_usd=10_000_000,
                reserved_nano_usd=12_000_000,
                input_tokens=10,
                output_tokens=3,
            ),
            _attempt(
                attempt_id="attempt_dev_2",
                scope_id="judge_scope_dev",
                call_id="call_dev",
                usage_status="unknown",
                actual_cost_nano_usd=None,
                reserved_nano_usd=5_000_000,
                input_tokens=None,
                output_tokens=None,
            ),
        ),
    )
    heldout = _job(
        phase="held_out_scoring",
        target_id="opaque_heldout_001",
        scope_id="judge_scope_heldout",
        attempts=(
            _attempt(
                attempt_id="attempt_heldout_1",
                scope_id="judge_scope_heldout",
                call_id="call_heldout",
                usage_status="known",
                actual_cost_nano_usd=7_000_000,
                reserved_nano_usd=9_000_000,
                input_tokens=6,
                output_tokens=2,
            ),
        ),
    )

    accounting = _account_judge_jobs((development,), (heldout,))

    assert accounting.development_job_count == 1
    assert accounting.heldout_job_count == 1
    assert accounting.completed_job_count == 2
    assert accounting.known_cost_usd == Decimal("0.017")
    assert accounting.unresolved_reserved_cost_usd == Decimal("0.005")
    assert accounting.effective_cost_usd == Decimal("0.022")
    assert accounting.input_tokens == 16
    assert accounting.output_tokens == 5
    assert accounting.total_tokens == 21
    assert accounting.generation_call_count == 2
    assert accounting.provider_attempt_count == 3
    assert accounting.retry_count == 1


def test_judge_accounting_rejects_reused_attempt_identity() -> None:
    first = _job(
        phase="development_calibration",
        target_id="calibration_dev_001",
        scope_id="judge_scope_dev",
        attempts=(_attempt(attempt_id="attempt_reused", scope_id="judge_scope_dev"),),
    )
    second = _job(
        phase="held_out_scoring",
        target_id="opaque_heldout_001",
        scope_id="judge_scope_heldout",
        attempts=(
            _attempt(attempt_id="attempt_reused", scope_id="judge_scope_heldout"),
        ),
    )

    with pytest.raises(
        Package8FinalizationErrorV3, match="judge_attempt_identity_duplicate"
    ):
        _account_judge_jobs((first,), (second,))


def test_report_manifest_hashes_every_report_file(tmp_path: Path) -> None:
    expected = (
        "summary.json",
        "preparation.json",
        "error-analysis.json",
        "comparisons.csv",
        "comparisons.svg",
    )
    for name in expected:
        (tmp_path / name).write_text(name, encoding="utf-8")

    _write_report_manifest(tmp_path)

    payload = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert tuple(item["path"] for item in payload["files"]) == expected
    assert len(payload["manifest_sha256"]) == 64


def test_postprocessing_source_manifest_is_sha256() -> None:
    assert len(package8_postprocessing_source_sha256_v3()) == 64


def _job(
    *,
    phase: JudgePhaseV3,
    target_id: str,
    scope_id: str,
    attempts: tuple[dict[str, object], ...],
) -> JudgeJobResultV3:
    identity = JudgeJobIdentityV3(
        phase=phase,
        target_id=target_id,
        source_sha256="a" * 64,
        run_key_sha256="b" * 64,
    )
    return JudgeJobResultV3(
        identity=identity,
        status="completed",
        record=cast(JudgeRecordV3, object()),
        error_code=None,
        scope_id=scope_id,
        ledger_attempts=attempts,
        replayed=False,
    )


def _attempt(
    *,
    attempt_id: str,
    scope_id: str,
    call_id: str = "call_default",
    usage_status: str = "known",
    actual_cost_nano_usd: int | None = 1,
    reserved_nano_usd: int = 1,
    input_tokens: int | None = 1,
    output_tokens: int | None = 1,
) -> dict[str, object]:
    total_tokens = (
        None
        if input_tokens is None or output_tokens is None
        else input_tokens + output_tokens
    )
    return {
        "attempt_id": attempt_id,
        "scope_id": scope_id,
        "call_id": call_id,
        "operation": "generation",
        "purpose": "judge",
        "usage_status": usage_status,
        "reserved_nano_usd": reserved_nano_usd,
        "actual_cost_nano_usd": actual_cost_nano_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
