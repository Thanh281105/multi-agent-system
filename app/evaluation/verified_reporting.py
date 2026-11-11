"""Export existing v3 evidence without inventing measurements or conclusions."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerPacketV3,
    EvaluationReportV3,
    JudgmentCollectionV3,
    UnblindingKeyV3,
    validate_evaluation_artifacts_v3,
)
from app.evaluation.v3_models import JudgmentModeV3


def export_verified_report(
    bundle: Path,
    output: Path,
    *,
    baseline: str | None = None,
    candidate: str | None = None,
) -> dict[str, Any]:
    """Copy validated evidence, preserving all comparisons including losses/nulls.

    This is an integrity-checked export, not an independent replication, judge
    calibration freeze, preregistration, or proof of an isolated ablation.
    """
    if output.exists():
        raise FileExistsError("output already exists; choose a new directory")
    if output.resolve().is_relative_to(bundle.resolve()):
        raise ValueError("output must be outside the source evidence bundle")
    if (baseline is None) != (candidate is None):
        raise ValueError("baseline and candidate must be supplied together")
    manifest = validate_evaluation_artifacts_v3(bundle)
    if manifest.completion_status != "complete":
        raise ValueError("partial evidence cannot be exported as a complete report")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".verified-report-", dir=output.parent))
    try:
        evidence = staging / "evidence"
        evidence.mkdir()
        for name in ("manifest.json", *(item.path for item in manifest.files)):
            shutil.copyfile(bundle / name, evidence / name)
        # Revalidate the copied bytes, not a source that may have changed.
        copied = validate_evaluation_artifacts_v3(
            evidence, expected_bindings=manifest.bindings
        )
        if copied != manifest:
            raise ValueError("source evidence changed during export")
        report = EvaluationReportV3.model_validate_json(
            (evidence / "report.json").read_bytes()
        )
        packet = BlindedAnswerPacketV3.model_validate_json(
            (evidence / "blinded_answers.json").read_bytes()
        )
        judgments = JudgmentCollectionV3.model_validate_json(
            (evidence / "judgments.json").read_bytes()
        )
        key = UnblindingKeyV3.model_validate_json(
            (evidence / "unblinding_key.json").read_bytes()
        )
        expected = {answer.opaque_answer_id for answer in packet.answers}
        actual = [judgment.opaque_answer_id for judgment in judgments.judgments]
        if not expected or len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("complete report requires exactly one judgment per answer")
        if any(
            item.judgment_mode == JudgmentModeV3.MODEL_JUDGE
            and item.calibration_sha256 is None
            for item in judgments.judgments
        ):
            raise ValueError("model judgments require recorded calibration bindings")
        comparisons = [
            item.model_dump(mode="json")
            for item in report.comparisons
            if baseline is None
            or (
                item.baseline_variant_id == baseline
                and item.candidate_variant_id == candidate
            )
        ]
        if not comparisons:
            raise ValueError("requested comparison is absent from verified evidence")
        scores = {item.opaque_answer_id: item.scores for item in judgments.judgments}
        reconciliation = []
        for comparison in comparisons:
            relevant = [
                entry
                for entry in key.entries
                if entry.variant_id
                in {
                    comparison["baseline_variant_id"],
                    comparison["candidate_variant_id"],
                }
            ]
            reconciliation.append(
                {
                    "baseline_variant_id": comparison["baseline_variant_id"],
                    "candidate_variant_id": comparison["candidate_variant_id"],
                    "metric": comparison["metric"],
                    "status": (
                        "recomputed_from_final_judgments"
                        if relevant
                        and all(
                            comparison["metric"] in scores[entry.opaque_answer_id]
                            for entry in relevant
                        )
                        else "artifact_arithmetic_only_no_judgment_metric"
                    ),
                }
            )
        summary: dict[str, Any] = {
            "schema_version": "verified_report_export_v2",
            "status": "complete_evidence_export",
            "evidence_classification": "not_independently_classified",
            "validation_scope": (
                "artifact_integrity_arithmetic_and_available_judgment_scores"
            ),
            "inference_status": (
                "recomputed_declared_bootstrap_no_new_significance_claim"
            ),
            "comparison_reconciliation": reconciliation,
            "execution_receipts_replayed": False,
            "attribution_status": "comparison_not_proof_of_isolated_component_effect",
            "run_id": report.run_id,
            "source_manifest_sha256": manifest.manifest_sha256,
            "bindings": manifest.bindings.model_dump(mode="json"),
            "accepted_observation_count": report.accepted_observation_count,
            "expected_observation_count": report.expected_observation_count,
            "comparisons": comparisons,
        }
        (staging / "summary.json").write_bytes(canonical_json_bytes(summary))
        lines = [
            "# Verified evidence export",
            "",
            "This export preserves validated source measurements and intervals.",
            "It does not establish independent validation, authentic human collection,",
            "or an isolated causal effect of an architectural component.",
            "Null metrics remain unmeasured; no default values are substituted.",
            "Declared bootstrap intervals and comparison arithmetic are recomputed.",
            "Available judgment metrics are rejoined; see comparison_reconciliation",
            "in summary.json. Execution receipts and provider costs are not replayed.",
            "",
            "| Metric | Baseline | Candidate | Candidate - baseline | 95% CI |",
            "|---|---|---|---:|---|",
        ]
        for item in comparisons:
            interval = item["confidence_interval"]
            bounds = (
                f"[{interval['lower']}, {interval['upper']}]"
                if interval is not None
                else "not measured"
            )
            delta = item["mean_delta"]
            value = str(delta) if delta is not None else "not measured"
            lines.append(
                f"| {item['metric']} | {item['baseline_variant_id']} | "
                f"{item['candidate_variant_id']} | {value} | {bounds} |"
            )
        (staging / "report.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
        files = [
            {
                "path": path.relative_to(staging).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        ]
        package = {"schema_version": "verified_report_package_v1", "files": files}
        package["manifest_sha256"] = canonical_sha256(package)
        (staging / "package-manifest.json").write_bytes(canonical_json_bytes(package))
        staging.rename(output)
        return summary
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
