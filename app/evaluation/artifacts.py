"""Atomic, content-addressed artifact bundles for evaluation v2."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.evaluation.comparison import compare_variants, summarize_robustness
from app.evaluation.protocol import (
    canonical_json_bytes,
    canonical_sha256,
    estimate_observation_cost,
    protocol_sha256,
    validate_observation_protocol,
)
from app.evaluation.v2_models import (
    ArtifactFileV2,
    ComparisonOmissionV2,
    EvaluationBundleManifestV2,
    EvaluationObservationV2,
    EvaluationPhase,
    EvaluationProtocolV2,
    PairedComparisonV2,
    PricingManifestV2,
    RobustnessSummaryV2,
)

_MANIFEST_NAME = "manifest.json"


def write_bundle(
    output_directory: Path,
    *,
    run_id: str,
    protocol: EvaluationProtocolV2,
    observations: Sequence[EvaluationObservationV2],
    comparisons: Sequence[PairedComparisonV2],
    omissions: Sequence[ComparisonOmissionV2] = (),
    robustness: Sequence[RobustnessSummaryV2] = (),
    pricing: PricingManifestV2 | None = None,
    created_at: datetime,
) -> EvaluationBundleManifestV2:
    if output_directory.exists():
        raise FileExistsError(f"evaluation output already exists: {output_directory}")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("bundle timestamp must be timezone-aware")

    protocol_hash = protocol_sha256(protocol)
    if (pricing is None) != (protocol.pricing_sha256 is None):
        raise ValueError("protocol and bundle must declare pricing together")
    if pricing is not None and canonical_sha256(pricing) != protocol.pricing_sha256:
        raise ValueError("pricing manifest hash does not match protocol")
    _validate_bundle_inputs(
        run_id=run_id,
        protocol=protocol,
        protocol_hash=protocol_hash,
        observations=observations,
        comparisons=comparisons,
        omissions=omissions,
        robustness=robustness,
        pricing=pricing,
    )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}-staging-",
            dir=output_directory.parent,
        )
    )
    try:
        files: list[ArtifactFileV2] = []
        files.append(_write_json(staging, "protocol.json", protocol))
        files.append(_write_json_lines(staging, "observations.jsonl", observations))
        files.append(_write_json(staging, "comparisons.json", list(comparisons)))
        files.append(_write_json(staging, "omissions.json", list(omissions)))
        files.append(_write_json(staging, "robustness.json", list(robustness)))
        if pricing is not None:
            files.append(_write_json(staging, "pricing.json", pricing))
        report = _report_payload(
            run_id=run_id,
            protocol=protocol,
            protocol_hash=protocol_hash,
            observations=observations,
            comparisons=comparisons,
            omissions=omissions,
            robustness=robustness,
        )
        files.append(_write_json(staging, "report.json", report))

        manifest = EvaluationBundleManifestV2(
            run_id=run_id,
            protocol_sha256=protocol_hash,
            created_at=created_at,
            git_revision=protocol.git_revision,
            git_dirty=protocol.git_dirty,
            observation_count=len(observations),
            comparison_count=len(comparisons),
            omission_count=len(omissions),
            files=tuple(files),
        )
        _write_bytes(staging / _MANIFEST_NAME, canonical_json_bytes(manifest))
        staging.rename(output_directory)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_bundle(output_directory: Path) -> EvaluationBundleManifestV2:
    if output_directory.is_symlink() or not output_directory.is_dir():
        raise ValueError("evaluation bundle directory is missing or unsafe")
    manifest_path = output_directory / _MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("evaluation manifest is missing or unsafe")
    manifest = EvaluationBundleManifestV2.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    expected_paths = {entry.path for entry in manifest.files}
    required_paths = {
        "protocol.json",
        "observations.jsonl",
        "comparisons.json",
        "omissions.json",
        "robustness.json",
        "report.json",
    }
    if not required_paths.issubset(expected_paths):
        raise ValueError("evaluation manifest omits required artifacts")
    allowed_paths = required_paths | {"pricing.json"}
    if not expected_paths.issubset(allowed_paths):
        raise ValueError("evaluation manifest declares unexpected artifacts")
    actual_paths = {
        path.relative_to(output_directory).as_posix()
        for path in output_directory.iterdir()
    }
    if actual_paths != expected_paths | {_MANIFEST_NAME}:
        raise ValueError("evaluation bundle contains missing or unexpected files")

    entries = {entry.path: entry for entry in manifest.files}
    for relative_path, entry in entries.items():
        path = output_directory / relative_path
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"bundle artifact is missing or unsafe: {relative_path}")
        payload = path.read_bytes()
        if len(payload) != entry.size_bytes:
            raise ValueError(f"bundle artifact size mismatch: {relative_path}")
        if hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise ValueError(f"bundle artifact hash mismatch: {relative_path}")

    protocol = EvaluationProtocolV2.model_validate_json(
        (output_directory / "protocol.json").read_text(encoding="utf-8")
    )
    if protocol_sha256(protocol) != manifest.protocol_sha256:
        raise ValueError("bundle protocol hash mismatch")
    pricing_path = output_directory / "pricing.json"
    pricing: PricingManifestV2 | None = None
    if (protocol.pricing_sha256 is None) != ("pricing.json" not in expected_paths):
        raise ValueError("bundle pricing presence does not match protocol")
    if protocol.pricing_sha256 is not None:
        pricing = PricingManifestV2.model_validate_json(
            pricing_path.read_text(encoding="utf-8")
        )
        if canonical_sha256(pricing) != protocol.pricing_sha256:
            raise ValueError("bundle pricing hash mismatch")

    observation_lines = [
        line
        for line in (output_directory / "observations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    if len(observation_lines) != manifest.observation_count:
        raise ValueError("bundle observation count mismatch")
    for line in observation_lines:
        observation = EvaluationObservationV2.model_validate_json(line)
        if observation.run_id != manifest.run_id:
            raise ValueError("bundle observation run ID mismatch")
        if observation.protocol_sha256 != manifest.protocol_sha256:
            raise ValueError("bundle observation protocol hash mismatch")
        validate_observation_protocol(observation, protocol)

    comparisons_payload = json.loads(
        (output_directory / "comparisons.json").read_text(encoding="utf-8")
    )
    comparisons = [
        PairedComparisonV2.model_validate(item) for item in comparisons_payload
    ]
    if len(comparisons) != manifest.comparison_count:
        raise ValueError("bundle comparison count mismatch")
    omissions = tuple(
        ComparisonOmissionV2.model_validate(item)
        for item in json.loads(
            (output_directory / "omissions.json").read_text(encoding="utf-8")
        )
    )
    if len(omissions) != manifest.omission_count:
        raise ValueError("bundle omission count mismatch")
    observations = tuple(
        EvaluationObservationV2.model_validate_json(line) for line in observation_lines
    )
    robustness = tuple(
        RobustnessSummaryV2.model_validate(item)
        for item in json.loads(
            (output_directory / "robustness.json").read_text(encoding="utf-8")
        )
    )
    _validate_bundle_inputs(
        run_id=manifest.run_id,
        protocol=protocol,
        protocol_hash=manifest.protocol_sha256,
        observations=observations,
        comparisons=comparisons,
        omissions=omissions,
        robustness=robustness,
        pricing=pricing,
    )
    report = json.loads((output_directory / "report.json").read_text(encoding="utf-8"))
    expected_report = _report_payload(
        run_id=manifest.run_id,
        protocol=protocol,
        protocol_hash=manifest.protocol_sha256,
        observations=observations,
        comparisons=comparisons,
        omissions=omissions,
        robustness=robustness,
    )
    if report != expected_report:
        raise ValueError("bundle report does not match verified artifacts")
    return manifest


def _validate_bundle_inputs(
    *,
    run_id: str,
    protocol: EvaluationProtocolV2,
    protocol_hash: str,
    observations: Sequence[EvaluationObservationV2],
    comparisons: Sequence[PairedComparisonV2],
    omissions: Sequence[ComparisonOmissionV2],
    robustness: Sequence[RobustnessSummaryV2],
    pricing: PricingManifestV2 | None,
) -> None:
    observation_keys: set[tuple[str, str, EvaluationPhase, int]] = set()
    execution_orders: set[int] = set()
    for observation in observations:
        if observation.run_id != run_id:
            raise ValueError("observation run ID does not match bundle")
        if observation.protocol_sha256 != protocol_hash:
            raise ValueError("observation protocol hash does not match bundle")
        validate_observation_protocol(observation, protocol)
        key = (
            observation.variant_id,
            observation.case_id,
            observation.phase,
            observation.repetition,
        )
        if key in observation_keys:
            raise ValueError(f"duplicate observation key: {key}")
        observation_keys.add(key)
        if observation.execution_order in execution_orders:
            raise ValueError("observation execution orders must be unique")
        execution_orders.add(observation.execution_order)
        if pricing is None:
            if (
                observation.estimated_cost_usd is not None
                or observation.cost_unavailable_reason is not None
            ):
                raise ValueError("unpriced observations cannot declare cost accounting")
        else:
            estimate = estimate_observation_cost(observation, pricing)
            if (
                observation.estimated_cost_usd != estimate.value_usd
                or observation.cost_unavailable_reason != estimate.unavailable_reason
            ):
                raise ValueError(
                    "observation cost accounting does not match pinned pricing"
                )
    expected_keys = {
        (variant.variant_id, case_id, phase, repetition)
        for variant in protocol.variants
        for phase, repeats, phase_cases in (
            (
                EvaluationPhase.CORRECTNESS,
                protocol.correctness_repeats,
                protocol.case_order,
            ),
            (
                EvaluationPhase.LATENCY,
                protocol.latency_repeats,
                protocol.effective_latency_case_order,
            ),
        )
        for case_id in phase_cases
        for repetition in range(repeats)
    }
    if observation_keys != expected_keys:
        raise ValueError(
            "complete bundle observation matrix does not match protocol: "
            f"missing={len(expected_keys - observation_keys)}, "
            f"unexpected={len(observation_keys - expected_keys)}"
        )
    if execution_orders != set(range(len(observations))):
        raise ValueError("observation execution orders must be contiguous from zero")
    known_variants = {variant.variant_id for variant in protocol.variants}
    comparison_keys: set[tuple[str, str, str, EvaluationPhase]] = set()
    for comparison in comparisons:
        if comparison.protocol_sha256 != protocol_hash:
            raise ValueError("comparison protocol hash does not match bundle")
        if not {
            comparison.baseline_variant_id,
            comparison.candidate_variant_id,
        }.issubset(known_variants):
            raise ValueError("comparison references unknown protocol variant")
        comparison_key = (
            comparison.baseline_variant_id,
            comparison.candidate_variant_id,
            comparison.metric.value,
            comparison.phase,
        )
        if comparison_key in comparison_keys:
            raise ValueError("bundle contains duplicate comparisons")
        comparison_keys.add(comparison_key)
        recomputed = compare_variants(
            observations,
            baseline_variant_id=comparison.baseline_variant_id,
            candidate_variant_id=comparison.candidate_variant_id,
            metric=comparison.metric,
            phase=comparison.phase,
            bootstrap_samples=comparison.confidence_interval.bootstrap_samples,
            random_seed=comparison.confidence_interval.random_seed,
        )
        if recomputed != comparison:
            raise ValueError("comparison does not match bundled observations")
    for omission in omissions:
        if omission.protocol_sha256 != protocol_hash:
            raise ValueError("comparison omission protocol hash does not match bundle")
        if not {
            omission.baseline_variant_id,
            omission.candidate_variant_id,
        }.issubset(known_variants):
            raise ValueError("comparison omission references unknown variant")
        omission_key = (
            omission.baseline_variant_id,
            omission.candidate_variant_id,
            omission.metric.value,
            omission.phase,
        )
        if omission_key in comparison_keys:
            raise ValueError("bundle duplicates a comparison or omission")
        comparison_keys.add(omission_key)
        try:
            compare_variants(
                observations,
                baseline_variant_id=omission.baseline_variant_id,
                candidate_variant_id=omission.candidate_variant_id,
                metric=omission.metric,
                phase=omission.phase,
                bootstrap_samples=protocol.bootstrap_samples,
                random_seed=protocol.random_seed,
            )
        except ValueError as exc:
            if "no comparable paired values" not in str(exc):
                raise ValueError("comparison omission has an invalid reason") from exc
        else:
            raise ValueError("comparison omission hides available paired values")
    for summary in robustness:
        if summary.protocol_sha256 != protocol_hash:
            raise ValueError("robustness protocol hash does not match bundle")
        if summary.variant_id not in known_variants:
            raise ValueError("robustness summary references unknown protocol variant")
        if summarize_robustness(observations, variant_id=summary.variant_id) != summary:
            raise ValueError("robustness summary does not match bundled observations")


def _report_payload(
    *,
    run_id: str,
    protocol: EvaluationProtocolV2,
    protocol_hash: str,
    observations: Sequence[EvaluationObservationV2],
    comparisons: Sequence[PairedComparisonV2],
    omissions: Sequence[ComparisonOmissionV2],
    robustness: Sequence[RobustnessSummaryV2],
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "run_id": run_id,
        "protocol_sha256": protocol_hash,
        "variant_ids": [variant.variant_id for variant in protocol.variants],
        "observation_count": len(observations),
        "comparison_count": len(comparisons),
        "omission_count": len(omissions),
        "robustness_summary_count": len(robustness),
        "comparisons": [item.model_dump(mode="json") for item in comparisons],
        "omissions": [item.model_dump(mode="json") for item in omissions],
        "robustness": [item.model_dump(mode="json") for item in robustness],
    }


def _write_json(
    staging: Path,
    name: str,
    value: Any,
) -> ArtifactFileV2:
    if isinstance(value, list):
        payload = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in value
        ]
        record_count: int | None = len(value)
    else:
        payload = value
        record_count = None
    return _write_artifact(
        staging,
        name,
        canonical_json_bytes(payload),
        record_count=record_count,
    )


def _write_json_lines(
    staging: Path,
    name: str,
    values: Sequence[EvaluationObservationV2],
) -> ArtifactFileV2:
    payload = b"".join(canonical_json_bytes(item) for item in values)
    return _write_artifact(staging, name, payload, record_count=len(values))


def _write_artifact(
    staging: Path,
    name: str,
    payload: bytes,
    *,
    record_count: int | None,
) -> ArtifactFileV2:
    _write_bytes(staging / name, payload)
    return ArtifactFileV2(
        path=name,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        record_count=record_count,
    )


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
