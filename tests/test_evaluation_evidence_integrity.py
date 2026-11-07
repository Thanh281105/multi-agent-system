"""Behavioral regressions for evidence-backed reporting entry points."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.ablation_study import (
    AblationConfiguration,
    AblationMeasurement,
    AblationTarget,
    compute_ablation_delta,
)
from app.evaluation.evidence_integrity import audit_manifest, recover_line_endings
from app.evaluation.human_calibration import agreement, summarize_ratings
from app.evaluation.human_calibration import main as calibration_main
from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerArtifactsV3,
    BlindedAnswerPacketV3,
    BlindedAnswerV3,
    EvaluationReportV3,
    RubricContextV3,
    RuntimeRubricEvidenceV3,
    UnblindingKeyV3,
    build_evaluation_report_v3,
    build_judgment_record_v3,
    write_evaluation_artifacts_v3,
)
from app.evaluation.v3_comparison import (
    METRIC_DIRECTIONS_V3,
    ArtifactBindingsV3,
    ConfidenceIntervalV3,
    EvaluationAnalysisV3,
    PairedMetricComparisonV3,
    WorkGroupDeltaV3,
)
from app.evaluation.v3_models import (
    PACKAGE7_METRICS,
    PACKAGE7_VARIANT_ORDER,
    EvaluationMetricV3,
    JudgmentModeV3,
)
from app.evaluation.verified_reporting import export_verified_report

ROOT = Path(__file__).resolve().parents[1]


def measurement(rate: float, *, ablated: bool) -> AblationMeasurement:
    return AblationMeasurement(
        configuration=AblationConfiguration(
            target=AblationTarget.EVIDENCE_ADAPTIVE_PLANNING,
            is_ablated=ablated,
            description="Test-only measurement",
        ),
        task_count=10,
        task_completion_rate=rate,
        claim_support_rate=0.8,
        citation_precision=0.9,
        citation_coverage=0.8,
        mean_latency_seconds=1.0,
        p95_latency_seconds=2.0,
        mean_total_tokens=100,
        mean_cost_usd=0.01,
    )


def test_descriptive_effect_does_not_claim_statistical_superiority() -> None:
    result = compute_ablation_delta(
        measurement(0.9, ablated=False), measurement(0.5, ablated=True)
    )
    assert result["delta_task_completion_pp"] == 40.0
    assert result["is_statistically_superior"] is None
    assert result["inference_status"] == "not_tested"


def test_ablation_rejects_mismatched_component() -> None:
    from dataclasses import replace

    ablated = measurement(0.5, ablated=True)
    other = replace(
        ablated,
        configuration=replace(
            ablated.configuration, target=AblationTarget.GROUNDED_SYNTHESIS
        ),
    )
    with pytest.raises(ValueError, match="same component"):
        compute_ablation_delta(measurement(0.9, ablated=False), other)


@pytest.mark.parametrize("rate", [-0.1, 1.1, float("nan")])
def test_ablation_rejects_invalid_measurements(rate: float) -> None:
    with pytest.raises(ValueError):
        measurement(rate, ablated=False)


@pytest.mark.parametrize(
    "script",
    [
        "scripts/build_reproducibility_package.py",
        "scripts/run_architectural_ablation.py",
        "evaluation/v3/thesis-validation-v1/run_human_judge_calibration.py",
    ],
)
def test_reporting_cli_requires_explicit_input_and_never_writes_default_outputs(
    script: str, tmp_path: Path
) -> None:
    # --help must be safe even without credentials, DB, or import-time Settings.
    command_cwd = tmp_path / "command"
    command_cwd.mkdir()
    help_result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=command_cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--output" in help_result.stdout
    result = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=command_cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "required" in result.stderr
    assert not list(command_cwd.iterdir())


def _write_manifest(directory: Path, content: bytes = b"original\n") -> Path:
    directory.mkdir()
    (directory / "data.json").write_bytes(content)
    payload = {
        "schema_version": "test_fixture",
        "files": [
            {
                "path": "data.json",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        ],
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    path = directory / "manifest.json"
    path.write_bytes(canonical_json_bytes(payload))
    return path


def test_integrity_audit_detects_tampering_and_never_repairs_files(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path / "bundle")
    assert audit_manifest(manifest)["status"] == "valid"
    data = manifest.parent / "data.json"
    data.write_bytes(b"modified\n")
    before = manifest.read_bytes()
    report = audit_manifest(manifest)
    assert report["status"] == "invalid"
    assert "file_digest_mismatch" in {i["code"] for i in report["issues"]}
    assert manifest.read_bytes() == before
    assert data.read_bytes() == b"modified\n"


def test_integrity_audit_reports_missing_file_and_invalid_manifest_hash(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path / "bundle")
    (manifest.parent / "data.json").unlink()
    payload = json.loads(manifest.read_bytes())
    payload["manifest_sha256"] = "0" * 64
    manifest.write_bytes(canonical_json_bytes(payload))
    codes = {i["code"] for i in audit_manifest(manifest)["issues"]}
    assert codes == {"missing_or_unreadable_file", "manifest_digest_mismatch"}


def test_newline_recovery_restores_exact_frozen_bytes_without_touching_source(
    tmp_path: Path,
) -> None:
    original = b'{"fixture":true}\r\n'
    manifest = _write_manifest(tmp_path / "source", original)
    source = manifest.parent / "data.json"
    source.write_bytes(original.replace(b"\r\n", b"\n"))
    before = source.read_bytes()
    audit = audit_manifest(manifest)
    assert audit["line_ending_recovery_candidates"][0]["line_endings"] == "crlf"
    output = tmp_path / "recovered"
    assert recover_line_endings(manifest, output)["status"] == "valid"
    assert (output / "data.json").read_bytes() == original
    assert (output / "manifest.json").read_bytes() == manifest.read_bytes()
    assert source.read_bytes() == before


@pytest.mark.parametrize("missing", [False, True])
def test_newline_recovery_refuses_missing_or_non_newline_edits(
    tmp_path: Path, missing: bool
) -> None:
    manifest = _write_manifest(tmp_path / "source")
    source = manifest.parent / "data.json"
    if missing:
        source.unlink()
    else:
        source.write_bytes(b"unrelated bytes")
    with pytest.raises(ValueError):
        recover_line_endings(manifest, tmp_path / "recovered")
    assert not (tmp_path / "recovered").exists()


@pytest.mark.parametrize(
    "name", ["../outside.json", "C:/outside", "..\\outside", "/root"]
)
def test_integrity_audit_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"file_digests": {name: "a" * 64}}))
    report = audit_manifest(manifest)
    assert report["checked_files"] == []
    assert report["issues"] == [{"code": "unsafe_file_path", "path": "manifest.json"}]


def _blind_fixture() -> BlindedAnswerArtifactsV3:
    """Explicitly synthetic data for contract tests, unrelated to research results."""
    bindings = ArtifactBindingsV3(
        **{name: "a" * 64 for name in ArtifactBindingsV3.model_fields}
    )
    answers = tuple(
        BlindedAnswerV3(
            opaque_answer_id=f"answer_{index:024x}",
            prompt=("Synthetic fixture question",),
            answer="Synthetic fixture answer",
            citations=(),
            claims=(),
            runtime_evidence=RuntimeRubricEvidenceV3(
                outcome="answered",
                planned_capabilities=(),
                successful_capabilities=(),
                successful_step_ids=(),
                plan_revision_count=0,
                final_revision_added_read_step_ids=(),
                final_revision_reused_step_ids=(),
            ),
            rubric_context=RubricContextV3(
                answerability="answerable",
                required_response_mode="direct_answer",
                required_facts=(),
                forbidden_claims=(),
                expected_action_outcome="complete",
                expected_dialogue_outcome="answered",
                allowed_capabilities=(),
                required_capabilities=(),
                forbidden_capabilities=(),
            ),
        )
        for index in range(4)
    )
    packet_payload = {
        "schema_version": "3.0",
        "split": "held_out",
        "bindings": bindings.model_dump(mode="json"),
        "randomization_seed": 1,
        "answer_count": len(answers),
        "answers": [a.model_dump(mode="json") for a in answers],
    }
    packet = BlindedAnswerPacketV3.model_validate(
        {**packet_payload, "packet_sha256": canonical_sha256(packet_payload)}
    )
    key_payload = {
        "schema_version": "3.0",
        "bindings": bindings.model_dump(mode="json"),
        "blinded_packet_sha256": packet.packet_sha256,
        "entries": [
            {
                "opaque_answer_id": answer.opaque_answer_id,
                "observation_id": f"fixture_{index}",
                "variant_id": variant,
                "conversation_id": "fixture_case",
                "work_group_id": "fixture_group",
                "repetition": 0,
            }
            for index, (answer, variant) in enumerate(
                zip(answers, PACKAGE7_VARIANT_ORDER, strict=True)
            )
        ],
    }
    key = UnblindingKeyV3.model_validate(
        {**key_payload, "key_sha256": canonical_sha256(key_payload)}
    )
    return BlindedAnswerArtifactsV3(packet=packet, unblinding_key=key)


def _report_fixture(directory: Path, *, complete: bool = True) -> tuple[Any, ...]:
    blinded = _blind_fixture()
    comparisons = []
    for baseline, candidate in combinations(PACKAGE7_VARIANT_ORDER, 2):
        for metric in PACKAGE7_METRICS:
            measured = metric != EvaluationMetricV3.DOCUMENT_RECALL
            left_value = float(baseline == "sa_shared_tools_rag")
            right_value = float(candidate == "sa_shared_tools_rag")
            delta = right_value - left_value
            comparisons.append(
                PairedMetricComparisonV3(
                    baseline_variant_id=baseline,
                    candidate_variant_id=candidate,
                    metric=metric,
                    direction=METRIC_DIRECTIONS_V3[metric],
                    paired_work_group_count=int(measured),
                    paired_conversation_count=int(measured),
                    paired_observation_count=int(measured),
                    baseline_mean=left_value if measured else None,
                    candidate_mean=right_value if measured else None,
                    mean_delta=delta if measured else None,
                    median_delta=delta if measured else None,
                    confidence_interval=(
                        ConfidenceIntervalV3(
                            lower=delta,
                            upper=delta,
                            bootstrap_samples=100,
                            random_seed=1,
                        )
                        if measured
                        else None
                    ),
                    candidate_wins=(
                        int(measured and delta != 0)
                        if METRIC_DIRECTIONS_V3[metric] == "lower_is_better"
                        else 0
                    ),
                    ties=int(measured and delta == 0),
                    candidate_losses=(
                        int(measured and delta != 0)
                        if METRIC_DIRECTIONS_V3[metric] == "higher_is_better"
                        else 0
                    ),
                    work_groups=(
                        (
                            WorkGroupDeltaV3(
                                work_group_id="fixture_group",
                                conversation_count=1,
                                paired_repetition_count=1,
                                baseline_mean=left_value,
                                candidate_mean=right_value,
                                delta=delta,
                            ),
                        )
                        if measured
                        else ()
                    ),
                )
            )
    payload = {
        "schema_version": "3.0",
        "bindings": blinded.packet.bindings.model_dump(mode="json"),
        "run_id": "test_fixture_only",
        "completion_status": "complete" if complete else "partial",
        "bootstrap_method": "work_group_percentile_bootstrap",
        "bootstrap_samples": 100,
        "random_seed": 1,
        "expected_observation_count": 4 if complete else 5,
        "accepted_observation_count": 4,
        "missing_observation_ids": [] if complete else ["missing_fixture"],
        "issues": [],
        "comparisons": [c.model_dump(mode="json") for c in comparisons],
    }
    analysis = EvaluationAnalysisV3.model_validate(
        {**payload, "analysis_sha256": canonical_sha256(payload)}
    )
    judgments = tuple(
        build_judgment_record_v3(
            bindings=analysis.bindings,
            blinded_packet_sha256=blinded.packet.packet_sha256,
            opaque_answer_id=answer.opaque_answer_id,
            judgment_mode=JudgmentModeV3.HUMAN_REVIEW,
            reviewer_id="fixture_reviewer",
            scores={
                EvaluationMetricV3.TASK_COMPLETION: float(
                    answer.opaque_answer_id == "answer_000000000000000000000000"
                )
            },
        )
        for answer in blinded.packet.answers
    )
    write_evaluation_artifacts_v3(
        directory,
        analysis=analysis,
        report=build_evaluation_report_v3(analysis),
        blinded=blinded,
        judgments=judgments,
    )
    return analysis, build_evaluation_report_v3(analysis), blinded, judgments


def test_writer_refuses_rehashed_report_with_a_different_run(tmp_path: Path) -> None:
    analysis, report, blinded, judgments = _report_fixture(tmp_path / "source")
    payload = report.model_dump(mode="json", exclude={"report_sha256"})
    payload["run_id"] = "unrelated_run"
    changed = EvaluationReportV3.model_validate(
        {**payload, "report_sha256": canonical_sha256(payload)}
    )
    with pytest.raises(ValueError, match="report.*analysis"):
        write_evaluation_artifacts_v3(
            tmp_path / "wrong",
            analysis=analysis,
            report=changed,
            blinded=blinded,
            judgments=judgments,
        )
    assert not (tmp_path / "wrong").exists()


@pytest.mark.parametrize(
    "field", ["mean_delta", "candidate_wins", "confidence_interval"]
)
def test_comparison_checks_arithmetic_and_declared_bootstrap(
    tmp_path: Path, field: str
) -> None:
    analysis, _, _, _ = _report_fixture(tmp_path / "source")
    comparison = next(
        item for item in analysis.comparisons if item.metric == "task_completion"
    ).model_dump(mode="json")
    if field == "mean_delta":
        comparison[field] = 0.5
    elif field == "candidate_wins":
        comparison.update(candidate_wins=1, candidate_losses=0)
    else:
        comparison[field].update(lower=0, upper=0)
    with pytest.raises(ValueError):
        PairedMetricComparisonV3.model_validate(comparison)


def test_writer_rejects_analysis_that_disagrees_with_final_judgments(
    tmp_path: Path,
) -> None:
    analysis, report, blinded, judgments = _report_fixture(tmp_path / "source")
    first = judgments[0]
    changed = build_judgment_record_v3(
        bindings=first.bindings,
        blinded_packet_sha256=first.blinded_packet_sha256,
        opaque_answer_id=first.opaque_answer_id,
        judgment_mode=JudgmentModeV3.HUMAN_REVIEW,
        reviewer_id="fixture_reviewer",
        scores={EvaluationMetricV3.TASK_COMPLETION: 0.0},
    )
    with pytest.raises(ValueError, match="analysis metric values.*judgments"):
        write_evaluation_artifacts_v3(
            tmp_path / "contradiction",
            analysis=analysis,
            report=report,
            blinded=blinded,
            judgments=(changed, *judgments[1:]),
        )
    assert not (tmp_path / "contradiction").exists()


def test_historical_protocol_is_readable_without_approving_current_execution() -> None:
    from app.evaluation.v3_protocol import load_evaluation_experiment_v3
    from app.evaluation.v3_variant_runtime import (
        EvaluationV3RuntimeConfigurationError,
        package7_variant_policy,
    )

    source = ROOT / "evaluation/v3/experiment.v3.json"
    raw = json.loads(source.read_bytes())
    loaded = load_evaluation_experiment_v3(source)
    assert loaded.config.variants[0].generation_binding.reasoning_effort == "low"
    assert loaded.experiment_sha256 == canonical_sha256(raw)
    with pytest.raises(
        EvaluationV3RuntimeConfigurationError, match="generation_binding_drift"
    ):
        package7_variant_policy(loaded.config.variants[0])


def test_retired_runners_cannot_run_without_optional_packages_or_mutate_files(
    tmp_path: Path,
) -> None:
    manifest = json.loads((ROOT / "docs/legacy-scripts/manifest.json").read_bytes())
    for entry in manifest["files"]:
        archive = ROOT / "docs/legacy-scripts" / entry["path"]
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == entry["sha256"]
        script = ROOT / "scripts" / entry["path"].removesuffix(".txt")
        # Isolated stdlib-only Python prevents an unsafe legacy regression from
        # importing DB/provider packages while testing these admission notices.
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 2, (script.name, result.stderr)
        assert "Retired runner" in result.stderr


def test_verified_export_preserves_losses_unmeasured_metrics_and_source_bytes(
    tmp_path: Path,
) -> None:
    source, output = tmp_path / "source", tmp_path / "export"
    _report_fixture(source)
    result = export_verified_report(source, output)
    task = next(c for c in result["comparisons"] if c["metric"] == "task_completion")
    recall = next(c for c in result["comparisons"] if c["metric"] == "document_recall")
    assert task["mean_delta"] == -1
    assert recall["mean_delta"] is None
    assert result["execution_receipts_replayed"] is False
    status = {
        item["metric"]: item["status"] for item in result["comparison_reconciliation"]
    }
    assert status["task_completion"] == "recomputed_from_final_judgments"
    assert status["effective_cost_usd"] == "artifact_arithmetic_only_no_judgment_metric"
    for path in source.iterdir():
        assert (output / "evidence" / path.name).read_bytes() == path.read_bytes()
    assert audit_manifest(output / "package-manifest.json")["status"] == "valid"
    assert "not measured" in (output / "report.md").read_text()
    with pytest.raises(FileExistsError):
        export_verified_report(source, output)


@pytest.mark.parametrize(
    "field", ["comparisons", "run_id", "accepted_observation_count"]
)
def test_report_with_recomputed_hashes_must_still_match_analysis(
    tmp_path: Path, field: str
) -> None:
    source = tmp_path / "source"
    _report_fixture(source)
    report_path = source / "report.json"
    report = json.loads(report_path.read_bytes())
    if field == "comparisons":
        # Valid comparison, opposite result, unchanged referenced analysis.
        comparison = report["comparisons"][0]
        comparison.update(
            baseline_mean=0.0,
            candidate_mean=1.0,
            mean_delta=1.0,
            median_delta=1.0,
            candidate_wins=1,
            candidate_losses=0,
        )
        comparison["confidence_interval"].update(lower=1.0, upper=1.0)
        comparison["work_groups"][0].update(
            baseline_mean=0.0, candidate_mean=1.0, delta=1.0
        )
    elif field == "run_id":
        report["run_id"] = "different_run"
    else:
        report[field] += 1
    report.pop("report_sha256")
    report["report_sha256"] = canonical_sha256(report)
    content = canonical_json_bytes(report)
    report_path.write_bytes(content)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for item in manifest["files"]:
        if item["path"] == "report.json":
            item["sha256"] = hashlib.sha256(content).hexdigest()
            item["size_bytes"] = len(content)
    manifest.pop("manifest_sha256")
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(ValueError, match="report.*analysis"):
        export_verified_report(source, tmp_path / "export")
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("partial", [False, True])
def test_verified_export_rejects_corrupt_or_partial_evidence(
    tmp_path: Path, partial: bool
) -> None:
    source, output = tmp_path / "source", tmp_path / "export"
    _report_fixture(source, complete=not partial)
    if not partial:
        (source / "report.json").write_bytes(b"{}")
    with pytest.raises(ValueError):
        export_verified_report(source, output)
    assert not output.exists()


def _ratings_fixture(tmp_path: Path) -> tuple[Path, Path]:
    packet_path = tmp_path / "packet.json"
    packet_path.write_bytes(canonical_json_bytes(_blind_fixture().packet))
    rows = [
        {
            "opaque_answer_id": f"answer_{index:024x}",
            "metric": "claim_support",
            "reviewer_a_id": "reviewer_alpha",
            "reviewer_b_id": "reviewer_beta",
            "adjudicator_id": "reviewer_gamma",
            "human_a": a,
            "human_b": b,
            "judge": judge,
            "adjudicated": gold,
        }
        for index, (a, b, judge, gold) in enumerate(
            [(1, 1, 1, 1), (1, 0, 0, 1), (0, 0, 1, 0), (0, 0, 0, 0)]
        )
    ]
    ratings_path = tmp_path / "ratings.jsonl"
    ratings_path.write_bytes(b"".join(canonical_json_bytes(row) for row in rows))
    return packet_path, ratings_path


def test_calibration_summary_uses_submitted_rows_and_keeps_counts_consistent(
    tmp_path: Path,
) -> None:
    packet, ratings = _ratings_fixture(tmp_path)
    result = summarize_ratings(packet, ratings)
    metric = result["metrics"]["claim_support"]
    assert result["sample_size"] == 4
    assert metric["human_agreement"]["agreed"] == 3
    assert metric["human_agreement"]["raw_agreement_rate"] == 0.75
    assert metric["judge_vs_adjudicated"]["raw_agreement_rate"] == 0.5
    assert metric["confusion_matrix"] == {
        "true_positives": 1,
        "true_negatives": 1,
        "false_positives": 1,
        "false_negatives": 1,
    }
    assert result["status"] == "descriptive_only_not_calibration_freeze"
    assert agreement([1, 1], [1, 1])["cohens_kappa"] is None


@pytest.mark.parametrize(
    "change", ["duplicate", "unknown_answer", "same_reviewer", "bool_label"]
)
def test_calibration_rejects_invalid_submissions(tmp_path: Path, change: str) -> None:
    packet, ratings = _ratings_fixture(tmp_path)
    rows = [json.loads(line) for line in ratings.read_text().splitlines()]
    if change == "duplicate":
        rows.append(rows[0])
    elif change == "unknown_answer":
        rows[0]["opaque_answer_id"] = "answer_ffffffffffffffffffffffff"
    elif change == "same_reviewer":
        rows[0]["reviewer_b_id"] = rows[0]["reviewer_a_id"]
    else:
        rows[0]["human_a"] = True
    ratings.write_bytes(b"".join(canonical_json_bytes(row) for row in rows))
    with pytest.raises(ValueError):
        summarize_ratings(packet, ratings)


def test_calibration_cli_refuses_to_overwrite_evidence(tmp_path: Path) -> None:
    packet, ratings = _ratings_fixture(tmp_path)
    before = ratings.read_bytes()
    with pytest.raises(SystemExit) as error:
        calibration_main(
            [
                "--packet",
                str(packet),
                "--ratings",
                str(ratings),
                "--output",
                str(ratings),
            ]
        )
    assert error.value.code == 1
    assert ratings.read_bytes() == before
