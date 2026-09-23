"""Atomic, content-addressed persistence for Evaluation v3 calibration."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_gold import EvaluationSplitV3
from app.evaluation.v3_judge import (
    CalibrationFreezeV3,
    CalibrationRecordV3,
    CalibrationReferenceBundleV3,
    CalibrationThresholdsV3,
    ModelJudgeConfigurationV3,
    freeze_calibration_thresholds_v3,
)

_SHA256 = r"^[a-f0-9]{64}$"
_ARTIFACT_NAME = r"^[a-z][a-z0-9_.-]{1,127}$"
_MANIFEST_NAME = "manifest.json"


class FrozenCalibrationArtifactV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CalibrationRecordCollectionV3(FrozenCalibrationArtifactV3):
    schema_version: Literal["3.0"] = "3.0"
    split: Literal[EvaluationSplitV3.DEVELOPMENT] = EvaluationSplitV3.DEVELOPMENT
    protocol_sha256: str = Field(pattern=_SHA256)
    configuration_sha256: str = Field(pattern=_SHA256)
    thresholds_sha256: str = Field(pattern=_SHA256)
    reference_bundle_sha256: str = Field(pattern=_SHA256)
    expected_calibration_ids: tuple[str, ...] = Field(min_length=1)
    completion_status: Literal["partial", "complete"]
    records: tuple[CalibrationRecordV3, ...]
    records_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_collection(self) -> CalibrationRecordCollectionV3:
        expected_ids = tuple(sorted(set(self.expected_calibration_ids)))
        if expected_ids != self.expected_calibration_ids:
            raise ValueError("expected calibration IDs must be sorted and unique")
        record_ids = [item.calibration_id for item in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("calibration records contain duplicate IDs")
        if not set(record_ids).issubset(expected_ids):
            raise ValueError("calibration records contain a foreign ID")
        if any(
            item.protocol_sha256 != self.protocol_sha256
            or item.configuration_sha256 != self.configuration_sha256
            or item.thresholds_sha256 != self.thresholds_sha256
            for item in self.records
        ):
            raise ValueError("calibration record provenance differs from collection")
        is_complete = set(record_ids) == set(expected_ids)
        if (self.completion_status == "complete") != is_complete:
            raise ValueError("calibration record completion status is invalid")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"records_sha256"})
        )
        if self.records_sha256 != expected_hash:
            raise ValueError("calibration records canonical hash mismatch")
        return self


def build_calibration_record_collection_v3(
    configuration: ModelJudgeConfigurationV3,
    reference_bundle: CalibrationReferenceBundleV3,
    thresholds: CalibrationThresholdsV3,
    records: Sequence[CalibrationRecordV3],
    *,
    expected_calibration_ids: Sequence[str],
) -> CalibrationRecordCollectionV3:
    configuration = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    reference_bundle = CalibrationReferenceBundleV3.model_validate(
        reference_bundle.model_dump(mode="json")
    )
    thresholds = CalibrationThresholdsV3.model_validate(
        thresholds.model_dump(mode="json")
    )
    normalized_records = tuple(
        sorted(
            (
                CalibrationRecordV3.model_validate(item.model_dump(mode="json"))
                for item in records
            ),
            key=lambda item: item.calibration_id,
        )
    )
    expected_ids = tuple(sorted(expected_calibration_ids))
    if reference_bundle.protocol_sha256 != configuration.bindings.protocol_sha256:
        raise ValueError("calibration reference protocol drift detected")
    reference_by_id = {
        item.calibration_id: item for item in reference_bundle.references
    }
    if set(reference_by_id) != set(expected_ids):
        raise ValueError("calibration references differ from expected development set")
    for record in normalized_records:
        reference = reference_by_id.get(record.calibration_id)
        if reference is None or record.reference_sha256 != reference.reference_sha256:
            raise ValueError("calibration record references missing or foreign labels")
    record_ids = {item.calibration_id for item in normalized_records}
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.DEVELOPMENT,
        "protocol_sha256": configuration.bindings.protocol_sha256,
        "configuration_sha256": configuration.configuration_sha256,
        "thresholds_sha256": thresholds.thresholds_sha256,
        "reference_bundle_sha256": reference_bundle.reference_bundle_sha256,
        "expected_calibration_ids": expected_ids,
        "completion_status": (
            "complete" if record_ids == set(expected_ids) else "partial"
        ),
        "records": [item.model_dump(mode="json") for item in normalized_records],
    }
    return CalibrationRecordCollectionV3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        configuration_sha256=configuration.configuration_sha256,
        thresholds_sha256=thresholds.thresholds_sha256,
        reference_bundle_sha256=reference_bundle.reference_bundle_sha256,
        expected_calibration_ids=expected_ids,
        completion_status=(
            "complete" if record_ids == set(expected_ids) else "partial"
        ),
        records=normalized_records,
        records_sha256=canonical_sha256(payload),
    )


class CalibrationArtifactFileV3(FrozenCalibrationArtifactV3):
    path: str = Field(pattern=_ARTIFACT_NAME)
    sha256: str = Field(pattern=_SHA256)
    size_bytes: int = Field(ge=0)
    record_count: int | None = Field(default=None, ge=0)


class CalibrationArtifactManifestV3(FrozenCalibrationArtifactV3):
    schema_version: Literal["3.0"] = "3.0"
    split: Literal[EvaluationSplitV3.DEVELOPMENT] = EvaluationSplitV3.DEVELOPMENT
    protocol_sha256: str = Field(pattern=_SHA256)
    configuration_sha256: str = Field(pattern=_SHA256)
    thresholds_sha256: str = Field(pattern=_SHA256)
    reference_bundle_sha256: str = Field(pattern=_SHA256)
    records_sha256: str = Field(pattern=_SHA256)
    calibration_sha256: str | None = Field(default=None, pattern=_SHA256)
    completion_status: Literal["partial", "complete"]
    files: tuple[CalibrationArtifactFileV3, ...]
    manifest_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> CalibrationArtifactManifestV3:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("calibration manifest paths must be unique")
        has_freeze = "freeze.json" in paths
        if has_freeze != (self.calibration_sha256 is not None):
            raise ValueError("calibration manifest freeze binding is inconsistent")
        if (self.completion_status == "complete") != has_freeze:
            raise ValueError("only complete calibration artifacts may contain a freeze")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if self.manifest_sha256 != expected_hash:
            raise ValueError("calibration manifest canonical hash mismatch")
        return self


class CalibrationArtifactsV3(FrozenCalibrationArtifactV3):
    manifest: CalibrationArtifactManifestV3
    configuration: ModelJudgeConfigurationV3
    references: CalibrationReferenceBundleV3
    thresholds: CalibrationThresholdsV3
    records: CalibrationRecordCollectionV3
    freeze: CalibrationFreezeV3 | None = None


def write_calibration_artifacts_v3(
    output_directory: Path,
    *,
    configuration: ModelJudgeConfigurationV3,
    references: CalibrationReferenceBundleV3,
    thresholds: CalibrationThresholdsV3,
    records: CalibrationRecordCollectionV3,
    freeze: CalibrationFreezeV3 | None = None,
) -> CalibrationArtifactManifestV3:
    """Write one immutable calibration checkpoint using an atomic rename."""

    if output_directory.exists():
        raise FileExistsError(f"calibration output already exists: {output_directory}")
    configuration, references, thresholds, records, freeze = _validate_components(
        configuration, references, thresholds, records, freeze
    )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}-staging-",
            dir=output_directory.parent,
        )
    )
    try:
        items: list[tuple[str, BaseModel, int | None]] = [
            ("judge_configuration.json", configuration, None),
            ("references.json", references, len(references.references)),
            ("thresholds.json", thresholds, None),
            ("records.json", records, len(records.records)),
        ]
        if freeze is not None:
            items.append(
                ("freeze.json", freeze, len(freeze.development_record_sha256s))
            )
        files = tuple(
            _write_artifact(staging, name, value, record_count)
            for name, value, record_count in items
        )
        manifest_payload = {
            "schema_version": "3.0",
            "split": EvaluationSplitV3.DEVELOPMENT,
            "protocol_sha256": configuration.bindings.protocol_sha256,
            "configuration_sha256": configuration.configuration_sha256,
            "thresholds_sha256": thresholds.thresholds_sha256,
            "reference_bundle_sha256": references.reference_bundle_sha256,
            "records_sha256": records.records_sha256,
            "calibration_sha256": (
                freeze.calibration_sha256 if freeze is not None else None
            ),
            "completion_status": records.completion_status,
            "files": [item.model_dump(mode="json") for item in files],
        }
        manifest = CalibrationArtifactManifestV3(
            protocol_sha256=configuration.bindings.protocol_sha256,
            configuration_sha256=configuration.configuration_sha256,
            thresholds_sha256=thresholds.thresholds_sha256,
            reference_bundle_sha256=references.reference_bundle_sha256,
            records_sha256=records.records_sha256,
            calibration_sha256=(
                freeze.calibration_sha256 if freeze is not None else None
            ),
            completion_status=records.completion_status,
            files=files,
            manifest_sha256=canonical_sha256(manifest_payload),
        )
        _write_bytes(staging / _MANIFEST_NAME, canonical_json_bytes(manifest))
        staging.rename(output_directory)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_calibration_artifacts_v3(
    output_directory: Path,
    *,
    expected_protocol_sha256: str | None = None,
    expected_configuration_sha256: str | None = None,
    expected_thresholds_sha256: str | None = None,
    expected_reference_bundle_sha256: str | None = None,
    require_complete: bool = False,
) -> CalibrationArtifactsV3:
    if output_directory.is_symlink() or not output_directory.is_dir():
        raise ValueError("calibration artifact directory is missing or unsafe")
    manifest_path = output_directory / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("calibration artifact manifest is missing or unsafe")
    manifest = CalibrationArtifactManifestV3.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    expected_names = {
        "judge_configuration.json",
        "references.json",
        "thresholds.json",
        "records.json",
    }
    if manifest.completion_status == "complete":
        expected_names.add("freeze.json")
    if {item.path for item in manifest.files} != expected_names:
        raise ValueError("calibration artifact manifest file set is invalid")
    if {item.name for item in output_directory.iterdir()} != expected_names | {
        _MANIFEST_NAME
    }:
        raise ValueError("calibration directory contains missing or unexpected files")
    for item in manifest.files:
        path = output_directory / item.path
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"calibration artifact is missing or unsafe: {item.path}")
        payload = path.read_bytes()
        if len(payload) != item.size_bytes:
            raise ValueError(f"calibration artifact size mismatch: {item.path}")
        if hashlib.sha256(payload).hexdigest() != item.sha256:
            raise ValueError(f"calibration artifact hash mismatch: {item.path}")

    configuration = ModelJudgeConfigurationV3.model_validate_json(
        (output_directory / "judge_configuration.json").read_text(encoding="utf-8")
    )
    references = CalibrationReferenceBundleV3.model_validate_json(
        (output_directory / "references.json").read_text(encoding="utf-8")
    )
    thresholds = CalibrationThresholdsV3.model_validate_json(
        (output_directory / "thresholds.json").read_text(encoding="utf-8")
    )
    records = CalibrationRecordCollectionV3.model_validate_json(
        (output_directory / "records.json").read_text(encoding="utf-8")
    )
    freeze = (
        CalibrationFreezeV3.model_validate_json(
            (output_directory / "freeze.json").read_text(encoding="utf-8")
        )
        if manifest.completion_status == "complete"
        else None
    )
    configuration, references, thresholds, records, freeze = _validate_components(
        configuration, references, thresholds, records, freeze
    )
    component_hashes = (
        configuration.bindings.protocol_sha256,
        configuration.configuration_sha256,
        thresholds.thresholds_sha256,
        references.reference_bundle_sha256,
        records.records_sha256,
        freeze.calibration_sha256 if freeze is not None else None,
    )
    manifest_hashes = (
        manifest.protocol_sha256,
        manifest.configuration_sha256,
        manifest.thresholds_sha256,
        manifest.reference_bundle_sha256,
        manifest.records_sha256,
        manifest.calibration_sha256,
    )
    if manifest_hashes != component_hashes:
        raise ValueError("calibration manifest component hash drift detected")
    actual = {
        "protocol": manifest.protocol_sha256,
        "configuration": manifest.configuration_sha256,
        "thresholds": manifest.thresholds_sha256,
        "reference": manifest.reference_bundle_sha256,
    }
    expected = {
        "protocol": expected_protocol_sha256,
        "configuration": expected_configuration_sha256,
        "thresholds": expected_thresholds_sha256,
        "reference": expected_reference_bundle_sha256,
    }
    for label, expected_hash in expected.items():
        if expected_hash is not None and actual[label] != expected_hash:
            raise ValueError(f"calibration {label} hash drift detected")
    if require_complete and freeze is None:
        raise ValueError("complete calibration freeze is required")
    return CalibrationArtifactsV3(
        manifest=manifest,
        configuration=configuration,
        references=references,
        thresholds=thresholds,
        records=records,
        freeze=freeze,
    )


def _validate_components(
    configuration: ModelJudgeConfigurationV3,
    references: CalibrationReferenceBundleV3,
    thresholds: CalibrationThresholdsV3,
    records: CalibrationRecordCollectionV3,
    freeze: CalibrationFreezeV3 | None,
) -> tuple[
    ModelJudgeConfigurationV3,
    CalibrationReferenceBundleV3,
    CalibrationThresholdsV3,
    CalibrationRecordCollectionV3,
    CalibrationFreezeV3 | None,
]:
    configuration = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    references = CalibrationReferenceBundleV3.model_validate(
        references.model_dump(mode="json")
    )
    thresholds = CalibrationThresholdsV3.model_validate(
        thresholds.model_dump(mode="json")
    )
    records = CalibrationRecordCollectionV3.model_validate(
        records.model_dump(mode="json")
    )
    if references.protocol_sha256 != configuration.bindings.protocol_sha256:
        raise ValueError("calibration reference protocol drift detected")
    if (
        records.protocol_sha256 != configuration.bindings.protocol_sha256
        or records.configuration_sha256 != configuration.configuration_sha256
        or records.thresholds_sha256 != thresholds.thresholds_sha256
        or records.reference_bundle_sha256 != references.reference_bundle_sha256
    ):
        raise ValueError("calibration component hash drift detected")
    reference_by_id = {item.calibration_id: item for item in references.references}
    if set(reference_by_id) != set(records.expected_calibration_ids):
        raise ValueError("calibration references differ from expected development set")
    for record in records.records:
        reference = reference_by_id.get(record.calibration_id)
        if reference is None or record.reference_sha256 != reference.reference_sha256:
            raise ValueError("calibration record references missing or foreign labels")
    if records.completion_status == "partial":
        if freeze is not None:
            raise ValueError("partial calibration records cannot carry a freeze")
    else:
        if freeze is None:
            raise ValueError("complete calibration records require a freeze")
        freeze = CalibrationFreezeV3.model_validate(freeze.model_dump(mode="json"))
        expected_freeze = freeze_calibration_thresholds_v3(
            configuration,
            records.records,
            thresholds,
            references,
            expected_calibration_ids=records.expected_calibration_ids,
        )
        if freeze != expected_freeze:
            raise ValueError("calibration freeze differs from validated inputs")
    return configuration, references, thresholds, records, freeze


def _write_artifact(
    directory: Path,
    name: str,
    value: BaseModel,
    record_count: int | None,
) -> CalibrationArtifactFileV3:
    payload = canonical_json_bytes(value)
    _write_bytes(directory / name, payload)
    return CalibrationArtifactFileV3(
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
