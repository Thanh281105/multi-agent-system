"""Canonical reports and blinded-answer artifacts for Evaluation v3."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_comparison import (
    ArtifactBindingsV3,
    CompletionIssueV3,
    EvaluationAnalysisV3,
    PairedMetricComparisonV3,
)
from app.evaluation.v3_gold import (
    AnswerabilityV3,
    CatalogPointerSupportV3,
    EvaluationGoldV3,
    EvaluationSplitV3,
    GoldConversationV3,
    RequiredResponseModeV3,
    SourceExcerptSupportV3,
)
from app.evaluation.v3_models import (
    EvaluationMetricV3,
    EvaluationProtocolV3,
    GenerationBindingV3,
    JudgmentModeV3,
    ObservationIdentityV3,
    ScheduledTurnKindV3,
    VariantIdV3,
    canonical_observation_id_v3,
)
from app.evaluation.v3_protocol import evaluation_protocol_sha256_v3

_SHA256 = r"^[a-f0-9]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.-]{2,127}$"
_OPAQUE_ANSWER_ID = r"^answer_[a-f0-9]{24}$"
_ARTIFACT_NAME = r"^[a-z][a-z0-9_.-]{1,127}$"
_MANIFEST_NAME = "manifest.json"

ScalarV3 = str | int | float | bool | None


class FrozenArtifactContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CitationForReviewV3(FrozenArtifactContractV3):
    label: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=4_000)


class AnswerEvidenceInputV3(FrozenArtifactContractV3):
    """Local runner boundary; execution metadata stays out of the blind packet."""

    observation_id: str
    variant_id: VariantIdV3
    conversation_id: str
    work_group_id: str
    repetition: int = Field(ge=0)
    answer: str = Field(min_length=1, max_length=100_000)
    citations: tuple[CitationForReviewV3, ...] = ()


class RubricFactV3(FrozenArtifactContractV3):
    claim: str
    expected_value: ScalarV3
    evidence: str


class RubricContextV3(FrozenArtifactContractV3):
    answerability: AnswerabilityV3
    required_response_mode: RequiredResponseModeV3
    required_facts: tuple[RubricFactV3, ...]
    forbidden_claims: tuple[str, ...]
    expected_action_outcome: str


class BlindedAnswerV3(FrozenArtifactContractV3):
    opaque_answer_id: str = Field(pattern=_OPAQUE_ANSWER_ID)
    prompt: tuple[str, ...] = Field(min_length=1)
    answer: str
    citations: tuple[CitationForReviewV3, ...]
    rubric_context: RubricContextV3


class BlindedAnswerPacketV3(FrozenArtifactContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    randomization_seed: int = Field(ge=0, le=2**32 - 1)
    answer_count: int = Field(ge=0)
    answers: tuple[BlindedAnswerV3, ...]
    packet_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_packet(self) -> BlindedAnswerPacketV3:
        if self.answer_count != len(self.answers):
            raise ValueError("blinded answer count mismatch")
        answer_ids = [item.opaque_answer_id for item in self.answers]
        if len(answer_ids) != len(set(answer_ids)):
            raise ValueError("opaque answer IDs must be unique")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"packet_sha256"})
        )
        if self.packet_sha256 != expected_hash:
            raise ValueError("blinded answer packet canonical hash mismatch")
        return self


class UnblindingEntryV3(FrozenArtifactContractV3):
    opaque_answer_id: str = Field(pattern=_OPAQUE_ANSWER_ID)
    observation_id: str
    variant_id: VariantIdV3
    conversation_id: str
    work_group_id: str
    repetition: int = Field(ge=0)


class UnblindingKeyV3(FrozenArtifactContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    blinded_packet_sha256: str = Field(pattern=_SHA256)
    entries: tuple[UnblindingEntryV3, ...]
    key_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_key(self) -> UnblindingKeyV3:
        answer_ids = [item.opaque_answer_id for item in self.entries]
        observation_ids = [item.observation_id for item in self.entries]
        if len(answer_ids) != len(set(answer_ids)):
            raise ValueError("unblinding answer IDs must be unique")
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("unblinding observation IDs must be unique")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"key_sha256"})
        )
        if self.key_sha256 != expected_hash:
            raise ValueError("unblinding key canonical hash mismatch")
        return self


class BlindedAnswerArtifactsV3(FrozenArtifactContractV3):
    packet: BlindedAnswerPacketV3
    unblinding_key: UnblindingKeyV3

    @model_validator(mode="after")
    def validate_pair(self) -> BlindedAnswerArtifactsV3:
        if self.packet.bindings != self.unblinding_key.bindings:
            raise ValueError("blind packet and unblinding key bindings differ")
        if self.unblinding_key.blinded_packet_sha256 != self.packet.packet_sha256:
            raise ValueError("unblinding key references a different blind packet")
        packet_ids = {item.opaque_answer_id for item in self.packet.answers}
        key_ids = {item.opaque_answer_id for item in self.unblinding_key.entries}
        if packet_ids != key_ids:
            raise ValueError("blind packet and unblinding key answer IDs differ")
        return self


class JudgmentRecordV3(FrozenArtifactContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    blinded_packet_sha256: str = Field(pattern=_SHA256)
    opaque_answer_id: str = Field(pattern=_OPAQUE_ANSWER_ID)
    judgment_mode: Literal[JudgmentModeV3.MODEL_JUDGE, JudgmentModeV3.HUMAN_REVIEW]
    reviewer_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    model_binding: GenerationBindingV3 | None = None
    scores: dict[EvaluationMetricV3, float] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=4_000)
    judgment_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_attribution(self) -> JudgmentRecordV3:
        is_review = self.judgment_mode == JudgmentModeV3.HUMAN_REVIEW
        if is_review:
            if self.reviewer_id is None or self.model_binding is not None:
                raise ValueError(
                    "human_review requires a reviewer ID and forbids a model binding"
                )
        elif self.reviewer_id is not None or self.model_binding is None:
            raise ValueError(
                "model_judge requires a model binding and forbids a reviewer ID"
            )
        if any(value < 0 or value > 1 for value in self.scores.values()):
            raise ValueError("judgment scores must be between zero and one")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"judgment_sha256"})
        )
        if self.judgment_sha256 != expected_hash:
            raise ValueError("judgment canonical hash mismatch")
        return self


class JudgmentCollectionV3(FrozenArtifactContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    blinded_packet_sha256: str = Field(pattern=_SHA256)
    judgments: tuple[JudgmentRecordV3, ...]
    collection_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_collection(self) -> JudgmentCollectionV3:
        for item in self.judgments:
            if item.bindings != self.bindings:
                raise ValueError("judgment bindings differ from the collection")
            if item.blinded_packet_sha256 != self.blinded_packet_sha256:
                raise ValueError("judgment references a different blind packet")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"collection_sha256"})
        )
        if self.collection_sha256 != expected_hash:
            raise ValueError("judgment collection canonical hash mismatch")
        return self


class EvaluationReportV3(FrozenArtifactContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    run_id: str
    completion_status: Literal["partial", "complete"]
    analysis_sha256: str = Field(pattern=_SHA256)
    expected_observation_count: int = Field(ge=1)
    accepted_observation_count: int = Field(ge=0)
    missing_observation_ids: tuple[str, ...]
    issues: tuple[CompletionIssueV3, ...]
    comparisons: tuple[PairedMetricComparisonV3, ...]
    report_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_report(self) -> EvaluationReportV3:
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"report_sha256"})
        )
        if self.report_sha256 != expected_hash:
            raise ValueError("report canonical hash mismatch")
        return self


class ArtifactFileV3(FrozenArtifactContractV3):
    path: str = Field(pattern=_ARTIFACT_NAME)
    sha256: str = Field(pattern=_SHA256)
    size_bytes: int = Field(ge=0)
    record_count: int | None = Field(default=None, ge=0)


class EvaluationArtifactManifestV3(FrozenArtifactContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    completion_status: Literal["partial", "complete"]
    files: tuple[ArtifactFileV3, ...]
    manifest_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_manifest(self) -> EvaluationArtifactManifestV3:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact manifest paths must be unique")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if self.manifest_sha256 != expected_hash:
            raise ValueError("artifact manifest canonical hash mismatch")
        return self


def build_evaluation_report_v3(analysis: EvaluationAnalysisV3) -> EvaluationReportV3:
    """Render all comparisons without suppressing losses or partial-run evidence."""

    payload = {
        "schema_version": "3.0",
        "bindings": analysis.bindings.model_dump(mode="json"),
        "run_id": analysis.run_id,
        "completion_status": analysis.completion_status,
        "analysis_sha256": analysis.analysis_sha256,
        "expected_observation_count": analysis.expected_observation_count,
        "accepted_observation_count": analysis.accepted_observation_count,
        "missing_observation_ids": analysis.missing_observation_ids,
        "issues": [item.model_dump(mode="json") for item in analysis.issues],
        "comparisons": [item.model_dump(mode="json") for item in analysis.comparisons],
    }
    return EvaluationReportV3(
        bindings=analysis.bindings,
        run_id=analysis.run_id,
        completion_status=analysis.completion_status,
        analysis_sha256=analysis.analysis_sha256,
        expected_observation_count=analysis.expected_observation_count,
        accepted_observation_count=analysis.accepted_observation_count,
        missing_observation_ids=analysis.missing_observation_ids,
        issues=analysis.issues,
        comparisons=analysis.comparisons,
        report_sha256=canonical_sha256(payload),
    )


def build_blinded_answer_packet_v3(
    protocol: EvaluationProtocolV3,
    gold: EvaluationGoldV3,
    analysis: EvaluationAnalysisV3,
    answers: Sequence[AnswerEvidenceInputV3],
    *,
    random_seed: int,
) -> BlindedAnswerArtifactsV3:
    """Randomize answer presentation and keep execution identities in a separate key."""

    protocol_hash = evaluation_protocol_sha256_v3(protocol)
    if analysis.bindings.protocol_sha256 != protocol_hash:
        raise ValueError("analysis protocol binding does not match the protocol")
    if analysis.bindings.gold_sha256 != protocol.assets.gold_sha256:
        raise ValueError("analysis gold binding does not match the protocol")

    heldout = {
        item.conversation_id: item
        for item in gold.conversations
        if item.split == EvaluationSplitV3.HELD_OUT
    }
    matrix_width = len(heldout) * len(protocol.variants)
    repeat_count, remainder = divmod(
        analysis.expected_observation_count,
        matrix_width,
    )
    if remainder or repeat_count not in {2, 3}:
        raise ValueError("analysis expected matrix does not match frozen v3 repeats")
    expected_observation_ids: set[str] = set()
    for case in heldout.values():
        for variant in protocol.variants:
            for repetition in range(repeat_count):
                identity = ObservationIdentityV3(
                    run_id=analysis.run_id,
                    protocol_sha256=protocol_hash,
                    schedule_algorithm_id=protocol.schedule_algorithm_id,
                    turn_kind=ScheduledTurnKindV3.MEASURED,
                    variant_id=variant.variant_id,
                    case_id=case.conversation_id,
                    work_group_id=case.work_group_id,
                    repetition=repetition,
                )
                expected_observation_ids.add(canonical_observation_id_v3(identity))
    required_answer_ids = expected_observation_ids - set(
        analysis.missing_observation_ids
    )
    answer_ids = [item.observation_id for item in answers]
    if len(answer_ids) != len(set(answer_ids)):
        raise ValueError("answer evidence contains duplicate observation IDs")
    if set(answer_ids) != required_answer_ids:
        missing = sorted(required_answer_ids - set(answer_ids))
        foreign = sorted(set(answer_ids) - required_answer_ids)
        raise ValueError(
            "answer evidence does not match accepted analysis observations: "
            f"missing={missing}, foreign={foreign}"
        )

    validated: list[tuple[AnswerEvidenceInputV3, GoldConversationV3]] = []
    for answer in answers:
        gold_case = heldout.get(answer.conversation_id)
        if gold_case is None:
            raise ValueError("answer evidence references a non-held-out conversation")
        if answer.work_group_id != gold_case.work_group_id:
            raise ValueError("answer evidence work group does not match gold")
        identity = ObservationIdentityV3(
            run_id=analysis.run_id,
            protocol_sha256=protocol_hash,
            schedule_algorithm_id=protocol.schedule_algorithm_id,
            turn_kind=ScheduledTurnKindV3.MEASURED,
            variant_id=answer.variant_id,
            case_id=answer.conversation_id,
            work_group_id=answer.work_group_id,
            repetition=answer.repetition,
        )
        if answer.observation_id != canonical_observation_id_v3(identity):
            raise ValueError("answer evidence observation identity is not canonical")
        validated.append((answer, gold_case))

    generator = random.Random(random_seed)
    randomized = sorted(validated, key=lambda item: item[0].observation_id)
    generator.shuffle(randomized)
    opaque_ids: set[str] = set()
    packet_answers: list[BlindedAnswerV3] = []
    key_entries: list[UnblindingEntryV3] = []
    for answer, case in randomized:
        opaque_id = _opaque_answer_id(generator, opaque_ids)
        opaque_ids.add(opaque_id)
        packet_answers.append(
            BlindedAnswerV3(
                opaque_answer_id=opaque_id,
                prompt=tuple(turn.message for turn in case.user_turns),
                answer=answer.answer,
                citations=answer.citations,
                rubric_context=_rubric_context(case),
            )
        )
        key_entries.append(
            UnblindingEntryV3(
                opaque_answer_id=opaque_id,
                observation_id=answer.observation_id,
                variant_id=answer.variant_id,
                conversation_id=answer.conversation_id,
                work_group_id=answer.work_group_id,
                repetition=answer.repetition,
            )
        )

    packet_payload = {
        "schema_version": "3.0",
        "bindings": analysis.bindings.model_dump(mode="json"),
        "randomization_seed": random_seed,
        "answer_count": len(packet_answers),
        "answers": [item.model_dump(mode="json") for item in packet_answers],
    }
    _reject_blind_leaks(packet_payload, protocol, answers)
    packet = BlindedAnswerPacketV3(
        bindings=analysis.bindings,
        randomization_seed=random_seed,
        answer_count=len(packet_answers),
        answers=tuple(packet_answers),
        packet_sha256=canonical_sha256(packet_payload),
    )
    key_payload = {
        "schema_version": "3.0",
        "bindings": analysis.bindings.model_dump(mode="json"),
        "blinded_packet_sha256": packet.packet_sha256,
        "entries": [item.model_dump(mode="json") for item in key_entries],
    }
    unblinding_key = UnblindingKeyV3(
        bindings=analysis.bindings,
        blinded_packet_sha256=packet.packet_sha256,
        entries=tuple(key_entries),
        key_sha256=canonical_sha256(key_payload),
    )
    return BlindedAnswerArtifactsV3(
        packet=packet,
        unblinding_key=unblinding_key,
    )


def build_judgment_record_v3(
    *,
    bindings: ArtifactBindingsV3,
    blinded_packet_sha256: str,
    opaque_answer_id: str,
    judgment_mode: Literal[
        JudgmentModeV3.MODEL_JUDGE,
        JudgmentModeV3.HUMAN_REVIEW,
    ],
    scores: dict[EvaluationMetricV3, float],
    reviewer_id: str | None = None,
    model_binding: GenerationBindingV3 | None = None,
    notes: str | None = None,
) -> JudgmentRecordV3:
    payload = {
        "schema_version": "3.0",
        "bindings": bindings.model_dump(mode="json"),
        "blinded_packet_sha256": blinded_packet_sha256,
        "opaque_answer_id": opaque_answer_id,
        "judgment_mode": judgment_mode,
        "reviewer_id": reviewer_id,
        "model_binding": (
            None if model_binding is None else model_binding.model_dump(mode="json")
        ),
        "scores": scores,
        "notes": notes,
    }
    return JudgmentRecordV3(
        bindings=bindings,
        blinded_packet_sha256=blinded_packet_sha256,
        opaque_answer_id=opaque_answer_id,
        judgment_mode=judgment_mode,
        reviewer_id=reviewer_id,
        model_binding=model_binding,
        scores=scores,
        notes=notes,
        judgment_sha256=canonical_sha256(payload),
    )


def write_evaluation_artifacts_v3(
    output_directory: Path,
    *,
    analysis: EvaluationAnalysisV3,
    report: EvaluationReportV3,
    blinded: BlindedAnswerArtifactsV3,
    judgments: Sequence[JudgmentRecordV3] = (),
) -> EvaluationArtifactManifestV3:
    """Atomically write deterministic, self-hashed Evaluation v3 artifacts."""

    if output_directory.exists():
        raise FileExistsError(f"evaluation output already exists: {output_directory}")
    if report.bindings != analysis.bindings or report.analysis_sha256 != (
        analysis.analysis_sha256
    ):
        raise ValueError("report does not bind the supplied analysis")
    if blinded.packet.bindings != analysis.bindings:
        raise ValueError("blind artifacts do not bind the supplied analysis")
    collection = _judgment_collection(blinded.packet, judgments)

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}-staging-",
            dir=output_directory.parent,
        )
    )
    try:
        files = tuple(
            _write_artifact(staging, name, value, record_count=record_count)
            for name, value, record_count in (
                ("analysis.json", analysis, None),
                ("report.json", report, None),
                ("blinded_answers.json", blinded.packet, blinded.packet.answer_count),
                (
                    "unblinding_key.json",
                    blinded.unblinding_key,
                    len(blinded.unblinding_key.entries),
                ),
                ("judgments.json", collection, len(collection.judgments)),
            )
        )
        manifest_payload = {
            "schema_version": "3.0",
            "bindings": analysis.bindings.model_dump(mode="json"),
            "completion_status": analysis.completion_status,
            "files": [item.model_dump(mode="json") for item in files],
        }
        manifest = EvaluationArtifactManifestV3(
            bindings=analysis.bindings,
            completion_status=analysis.completion_status,
            files=files,
            manifest_sha256=canonical_sha256(manifest_payload),
        )
        _write_bytes(staging / _MANIFEST_NAME, canonical_json_bytes(manifest))
        staging.rename(output_directory)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_evaluation_artifacts_v3(
    output_directory: Path,
) -> EvaluationArtifactManifestV3:
    if output_directory.is_symlink() or not output_directory.is_dir():
        raise ValueError("evaluation artifact directory is missing or unsafe")
    manifest_path = output_directory / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("evaluation artifact manifest is missing or unsafe")
    manifest = EvaluationArtifactManifestV3.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    expected_names = {
        "analysis.json",
        "report.json",
        "blinded_answers.json",
        "unblinding_key.json",
        "judgments.json",
    }
    if {item.path for item in manifest.files} != expected_names:
        raise ValueError("artifact manifest file set is incomplete")
    actual_names = {item.name for item in output_directory.iterdir()}
    if actual_names != expected_names | {_MANIFEST_NAME}:
        raise ValueError("artifact directory contains missing or unexpected files")
    for item in manifest.files:
        path = output_directory / item.path
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"artifact is missing or unsafe: {item.path}")
        payload = path.read_bytes()
        if len(payload) != item.size_bytes:
            raise ValueError(f"artifact size mismatch: {item.path}")
        if hashlib.sha256(payload).hexdigest() != item.sha256:
            raise ValueError(f"artifact hash mismatch: {item.path}")

    analysis = EvaluationAnalysisV3.model_validate_json(
        (output_directory / "analysis.json").read_text(encoding="utf-8")
    )
    report = EvaluationReportV3.model_validate_json(
        (output_directory / "report.json").read_text(encoding="utf-8")
    )
    packet = BlindedAnswerPacketV3.model_validate_json(
        (output_directory / "blinded_answers.json").read_text(encoding="utf-8")
    )
    key = UnblindingKeyV3.model_validate_json(
        (output_directory / "unblinding_key.json").read_text(encoding="utf-8")
    )
    collection = JudgmentCollectionV3.model_validate_json(
        (output_directory / "judgments.json").read_text(encoding="utf-8")
    )
    if any(
        bindings != manifest.bindings
        for bindings in (
            analysis.bindings,
            report.bindings,
            packet.bindings,
            key.bindings,
            collection.bindings,
        )
    ):
        raise ValueError("artifact bindings differ from the manifest")
    if report.analysis_sha256 != analysis.analysis_sha256:
        raise ValueError("report references a different analysis")
    BlindedAnswerArtifactsV3(packet=packet, unblinding_key=key)
    if collection.blinded_packet_sha256 != packet.packet_sha256:
        raise ValueError("judgment collection references a different blind packet")
    return manifest


def _rubric_context(case: GoldConversationV3) -> RubricContextV3:
    required_facts = []
    for fact in case.required_fact_blueprints:
        support = fact.support
        if isinstance(support, SourceExcerptSupportV3):
            evidence = support.exact_excerpt
        elif isinstance(support, CatalogPointerSupportV3):
            evidence = (
                f"{support.artifact_path}{support.json_pointer}="
                f"{json.dumps(support.expected_value, ensure_ascii=False)}"
            )
        else:  # pragma: no cover - the gold union is closed by Pydantic
            raise TypeError("unsupported rubric evidence type")
        required_facts.append(
            RubricFactV3(
                claim=fact.claim_blueprint,
                expected_value=fact.expected_value,
                evidence=evidence,
            )
        )
    return RubricContextV3(
        answerability=case.answerability,
        required_response_mode=case.required_response_mode,
        required_facts=tuple(required_facts),
        forbidden_claims=tuple(
            fact.matcher_blueprint for fact in case.forbidden_fact_blueprints
        ),
        expected_action_outcome=(
            case.action_capability_blueprint.expected_outcome.value
        ),
    )


def _opaque_answer_id(generator: random.Random, existing: set[str]) -> str:
    while True:
        candidate = f"answer_{generator.getrandbits(96):024x}"
        if candidate not in existing:
            return candidate


def _reject_blind_leaks(
    payload: dict[str, object],
    protocol: EvaluationProtocolV3,
    answers: Sequence[AnswerEvidenceInputV3],
) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    forbidden_values = {
        *(variant.variant_id for variant in protocol.variants),
        *(variant.generation_binding.model for variant in protocol.variants),
        *(answer.observation_id for answer in answers),
        *(answer.conversation_id for answer in answers),
        *(answer.work_group_id for answer in answers),
    }
    forbidden_keys = {
        "variant_id",
        "observation_id",
        "conversation_id",
        "case_id",
        "work_group_id",
        "repetition",
        "execution_order",
        "agent_topology",
        "planning_mode",
        "model_binding",
        "known_cost_usd",
        "effective_cost_usd",
        "end_to_end_latency_ms",
        "total_tokens",
    }
    leaked_values = sorted(value for value in forbidden_values if value in serialized)
    leaked_keys = sorted(
        f'"{key}"' for key in forbidden_keys if f'"{key}"' in serialized
    )
    if leaked_values or leaked_keys:
        raise ValueError(
            "blinded answer packet contains identifying execution metadata: "
            f"values={leaked_values}, keys={leaked_keys}"
        )


def _judgment_collection(
    packet: BlindedAnswerPacketV3,
    judgments: Sequence[JudgmentRecordV3],
) -> JudgmentCollectionV3:
    packet_ids = {item.opaque_answer_id for item in packet.answers}
    judgment_keys: set[tuple[str, JudgmentModeV3, str | None]] = set()
    for item in judgments:
        if item.opaque_answer_id not in packet_ids:
            raise ValueError("judgment references an unknown opaque answer ID")
        key = (item.opaque_answer_id, item.judgment_mode, item.reviewer_id)
        if key in judgment_keys:
            raise ValueError("judgment collection contains duplicate attribution")
        judgment_keys.add(key)
    payload = {
        "schema_version": "3.0",
        "bindings": packet.bindings.model_dump(mode="json"),
        "blinded_packet_sha256": packet.packet_sha256,
        "judgments": [item.model_dump(mode="json") for item in judgments],
    }
    return JudgmentCollectionV3(
        bindings=packet.bindings,
        blinded_packet_sha256=packet.packet_sha256,
        judgments=tuple(judgments),
        collection_sha256=canonical_sha256(payload),
    )


def _write_artifact(
    directory: Path,
    name: str,
    value: BaseModel,
    *,
    record_count: int | None,
) -> ArtifactFileV3:
    payload = canonical_json_bytes(value)
    _write_bytes(directory / name, payload)
    return ArtifactFileV3(
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
