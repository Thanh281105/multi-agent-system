"""Calibrated, blinded semantic-judge boundary for Evaluation v3.

The provider boundary in this module accepts only a blinded answer and immutable
judge metadata.  Development calibration is represented separately and must be
frozen before a held-out packet can be scored.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerPacketV3,
    BlindedAnswerV3,
    ClaimCitationCoverageV3,
    ClaimCitationVerdictV3,
    JudgmentRecordV3,
    build_judgment_record_v3,
)
from app.evaluation.v3_comparison import ArtifactBindingsV3
from app.evaluation.v3_gold import EvaluationSplitV3
from app.evaluation.v3_matching import citation_supports_fact_v3, exact_value_in_text_v3
from app.evaluation.v3_models import (
    EvaluationMetricV3,
    GenerationBindingV3,
    JudgmentModeV3,
)

_SHA256 = r"^[a-f0-9]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.-]{2,127}$"

SEMANTIC_JUDGE_METRICS_V3: tuple[EvaluationMetricV3, ...] = (
    EvaluationMetricV3.TASK_COMPLETION,
    EvaluationMetricV3.ANSWERABILITY_ABSTENTION,
    EvaluationMetricV3.CLAIM_SUPPORT,
    EvaluationMetricV3.AUTHORIZATION,
    EvaluationMetricV3.VALID_PLAN,
    EvaluationMetricV3.USEFUL_CONTINUATION,
)
DETERMINISTIC_JUDGE_METRICS_V3: tuple[EvaluationMetricV3, ...] = (
    EvaluationMetricV3.CITATION_PRECISION,
    EvaluationMetricV3.CITATION_COVERAGE,
    EvaluationMetricV3.DOCUMENT_RECALL,
)
COMPLETE_JUDGED_METRICS_V3 = (
    *SEMANTIC_JUDGE_METRICS_V3,
    *DETERMINISTIC_JUDGE_METRICS_V3,
)


class FrozenJudgeContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SemanticMetricVerdictV3(FrozenJudgeContractV3):
    metric: Literal[
        EvaluationMetricV3.TASK_COMPLETION,
        EvaluationMetricV3.ANSWERABILITY_ABSTENTION,
        EvaluationMetricV3.CLAIM_SUPPORT,
        EvaluationMetricV3.AUTHORIZATION,
        EvaluationMetricV3.VALID_PLAN,
        EvaluationMetricV3.USEFUL_CONTINUATION,
    ]
    score: float = Field(strict=True, json_schema_extra={"enum": [0.0, 1.0]})
    rubric_fact_indices: tuple[int, ...] = ()
    citation_labels: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> SemanticMetricVerdictV3:
        if self.metric not in SEMANTIC_JUDGE_METRICS_V3:
            raise ValueError("model output contains a non-semantic metric")
        if self.score not in (0.0, 1.0):
            raise ValueError("semantic metric score must be binary")
        if len(self.rubric_fact_indices) != len(set(self.rubric_fact_indices)):
            raise ValueError("rubric fact references must be unique")
        if any(index < 0 for index in self.rubric_fact_indices):
            raise ValueError("rubric fact references must be non-negative")
        if len(self.citation_labels) != len(set(self.citation_labels)):
            raise ValueError("citation references must be unique")
        return self


class ModelJudgeOutputV3(FrozenJudgeContractV3):
    """Strict provider output with no free-form factual rationale field."""

    schema_version: Literal["3.0"] = "3.0"
    output_schema_sha256: str = Field(pattern=_SHA256)
    opaque_answer_id: str = Field(pattern=r"^answer_[a-f0-9]{24}$")
    verdicts: tuple[SemanticMetricVerdictV3, ...] = Field(
        min_length=len(SEMANTIC_JUDGE_METRICS_V3),
        max_length=len(SEMANTIC_JUDGE_METRICS_V3),
    )

    @model_validator(mode="after")
    def validate_complete_metric_set(self) -> ModelJudgeOutputV3:
        metrics = tuple(item.metric for item in self.verdicts)
        if set(metrics) != set(SEMANTIC_JUDGE_METRICS_V3):
            raise ValueError(
                "model output must contain every semantic metric exactly once"
            )
        expected_schema_hash = model_judge_output_schema_sha256_v3(
            successor=isinstance(self, ClaimCoverageModelJudgeOutputV3)
        )
        if self.output_schema_sha256 != expected_schema_hash:
            raise ValueError("model output schema hash mismatch")
        return self


class ClaimCoverageModelJudgeOutputV3(ModelJudgeOutputV3):
    claim_coverage_contract: Literal["receipt_claim_citation_coverage_v1"]
    claim_verdicts: tuple[ClaimCitationVerdictV3, ...]


def model_judge_output_schema_sha256_v3(*, successor: bool = False) -> str:
    schema = (
        ClaimCoverageModelJudgeOutputV3 if successor else ModelJudgeOutputV3
    ).model_json_schema()
    # The field itself is a binding to this schema, not part of a recursive hash.
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties = dict(properties)
        properties.pop("output_schema_sha256", None)
        schema = {**schema, "properties": properties}
    required = schema.get("required")
    if isinstance(required, list):
        schema = {
            **schema,
            "required": [item for item in required if item != "output_schema_sha256"],
        }
    return canonical_sha256(schema)


def model_judge_output_type_v3(schema_sha256: str) -> type[ModelJudgeOutputV3]:
    if schema_sha256 == model_judge_output_schema_sha256_v3(successor=True):
        return ClaimCoverageModelJudgeOutputV3
    if schema_sha256 == model_judge_output_schema_sha256_v3():
        return ModelJudgeOutputV3
    raise ValueError("judge output schema hash mismatch")


def judge_prompt_sha256_v3(prompt: str) -> str:
    return canonical_sha256({"judge_prompt": prompt})


def evaluator_configuration_sha256_v3(
    *,
    model_binding: GenerationBindingV3,
    judge_prompt_sha256: str,
    judge_schema_sha256: str,
    rubric_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "model_binding": model_binding.model_dump(mode="json"),
            "judge_prompt_sha256": judge_prompt_sha256,
            "judge_schema_sha256": judge_schema_sha256,
            "rubric_sha256": rubric_sha256,
            "semantic_metrics": [item.value for item in SEMANTIC_JUDGE_METRICS_V3],
            "deterministic_metrics": [
                item.value for item in DETERMINISTIC_JUDGE_METRICS_V3
            ],
        }
    )


class ModelJudgeConfigurationV3(FrozenJudgeContractV3):
    schema_version: Literal["3.0"] = "3.0"
    bindings: ArtifactBindingsV3
    model_binding: GenerationBindingV3
    judge_prompt: str = Field(min_length=1, max_length=50_000)
    semantic_metrics: tuple[EvaluationMetricV3, ...] = SEMANTIC_JUDGE_METRICS_V3
    deterministic_metrics: tuple[EvaluationMetricV3, ...] = (
        DETERMINISTIC_JUDGE_METRICS_V3
    )
    configuration_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_configuration(self) -> ModelJudgeConfigurationV3:
        if self.semantic_metrics != SEMANTIC_JUDGE_METRICS_V3:
            raise ValueError("semantic judge metrics differ from the frozen contract")
        if self.deterministic_metrics != DETERMINISTIC_JUDGE_METRICS_V3:
            raise ValueError("deterministic metrics differ from the frozen contract")
        prompt_hash = judge_prompt_sha256_v3(self.judge_prompt)
        if prompt_hash != self.bindings.judge_prompt_sha256:
            raise ValueError("judge prompt hash mismatch")
        model_judge_output_type_v3(self.bindings.judge_schema_sha256)
        schema_hash = self.bindings.judge_schema_sha256
        evaluator_hash = evaluator_configuration_sha256_v3(
            model_binding=self.model_binding,
            judge_prompt_sha256=prompt_hash,
            judge_schema_sha256=schema_hash,
            rubric_sha256=self.bindings.rubric_sha256,
        )
        if evaluator_hash != self.bindings.evaluator_configuration_sha256:
            raise ValueError("evaluator configuration hash mismatch")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"configuration_sha256"})
        )
        if self.configuration_sha256 != expected_hash:
            raise ValueError("judge configuration canonical hash mismatch")
        return self


def build_model_judge_configuration_v3(
    *,
    bindings: ArtifactBindingsV3,
    model_binding: GenerationBindingV3,
    judge_prompt: str,
) -> ModelJudgeConfigurationV3:
    payload = {
        "schema_version": "3.0",
        "bindings": bindings.model_dump(mode="json"),
        "model_binding": model_binding.model_dump(mode="json"),
        "judge_prompt": judge_prompt,
        "semantic_metrics": [item.value for item in SEMANTIC_JUDGE_METRICS_V3],
        "deterministic_metrics": [
            item.value for item in DETERMINISTIC_JUDGE_METRICS_V3
        ],
    }
    return ModelJudgeConfigurationV3(
        bindings=bindings,
        model_binding=model_binding,
        judge_prompt=judge_prompt,
        configuration_sha256=canonical_sha256(payload),
    )


class DevelopmentCalibrationCaseV3(FrozenJudgeContractV3):
    schema_version: Literal["3.0"] = "3.0"
    split: Literal[EvaluationSplitV3.DEVELOPMENT] = EvaluationSplitV3.DEVELOPMENT
    calibration_id: str = Field(pattern=_IDENTIFIER)
    answer: BlindedAnswerV3
    case_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_case_hash(self) -> DevelopmentCalibrationCaseV3:
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"case_sha256"})
        )
        if self.case_sha256 != expected:
            raise ValueError("development calibration case hash mismatch")
        return self


def build_development_calibration_case_v3(
    *,
    calibration_id: str,
    answer: BlindedAnswerV3,
) -> DevelopmentCalibrationCaseV3:
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.DEVELOPMENT,
        "calibration_id": calibration_id,
        "answer": answer.model_dump(mode="json"),
    }
    return DevelopmentCalibrationCaseV3(
        calibration_id=calibration_id,
        answer=answer,
        case_sha256=canonical_sha256(payload),
    )


class ReferenceAdjudicatorV3(FrozenJudgeContractV3):
    """Declared independent label provenance, separate from the scored judge."""

    adjudicator_id: str = Field(pattern=_IDENTIFIER)
    method: Literal["automated_model", "human_review"]
    source_system: str = Field(min_length=1, max_length=200)
    model_snapshot: str | None = Field(default=None, min_length=1, max_length=200)
    instructions_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_method(self) -> ReferenceAdjudicatorV3:
        if (self.method == "automated_model") != (self.model_snapshot is not None):
            raise ValueError("automated label provenance requires a model snapshot")
        return self


class CalibrationReferenceLabelsV3(FrozenJudgeContractV3):
    """Provenance-bearing development labels; never implied to be human."""

    schema_version: Literal["3.0"] = "3.0"
    split: Literal[EvaluationSplitV3.DEVELOPMENT] = EvaluationSplitV3.DEVELOPMENT
    calibration_id: str = Field(pattern=_IDENTIFIER)
    development_observation_id: str = Field(min_length=1, max_length=256)
    development_case_sha256: str = Field(pattern=_SHA256)
    answer_sha256: str = Field(pattern=_SHA256)
    source_classification: Literal[
        "automated_gold_derived", "automated_independent_semantic", "human_review"
    ]
    reviewer_ids: tuple[str, ...] = ()
    adjudicator: ReferenceAdjudicatorV3 | None = None
    claim_coverage: ClaimCitationCoverageV3 | None = None
    scores: dict[EvaluationMetricV3, float]
    reference_sha256: str = Field(pattern=_SHA256)

    @model_serializer(mode="wrap")
    def preserve_legacy_reference_hash(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        payload = handler(self)
        for name in ("adjudicator", "claim_coverage"):
            if payload.get(name) is None:
                payload.pop(name, None)
        return payload

    @model_validator(mode="after")
    def validate_reference(self) -> CalibrationReferenceLabelsV3:
        _validate_complete_score_map(self.scores, label="reference calibration")
        if len(self.reviewer_ids) != len(set(self.reviewer_ids)) or any(
            re.fullmatch(_IDENTIFIER, reviewer_id) is None
            for reviewer_id in self.reviewer_ids
        ):
            raise ValueError("reference reviewer IDs must be nonempty and unique")
        if self.source_classification == "human_review":
            if not self.reviewer_ids:
                raise ValueError("human_review references require reviewer IDs")
        elif self.reviewer_ids:
            raise ValueError("automated references forbid reviewer IDs")
        if self.source_classification == "automated_independent_semantic":
            if self.adjudicator is None or self.adjudicator.method != "automated_model":
                raise ValueError(
                    "independent semantic labels require automated model provenance"
                )
        if self.adjudicator is not None and (
            (self.source_classification == "human_review")
            != (self.adjudicator.method == "human_review")
        ):
            raise ValueError(
                "reference label classification differs from adjudicator provenance"
            )
        if self.claim_coverage is not None and (
            self.claim_coverage.answer_sha256 != self.answer_sha256
        ):
            raise ValueError("reference claim labels use a different blinded answer")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"reference_sha256"})
        )
        if self.reference_sha256 != expected_hash:
            raise ValueError("calibration reference canonical hash mismatch")
        return self


def blinded_answer_sha256_v3(answer: BlindedAnswerV3) -> str:
    return canonical_sha256(answer)


def build_calibration_reference_labels_v3(
    case: DevelopmentCalibrationCaseV3,
    *,
    development_observation_id: str,
    source_classification: Literal[
        "automated_gold_derived", "automated_independent_semantic", "human_review"
    ],
    scores: Mapping[EvaluationMetricV3, float],
    reviewer_ids: Sequence[str] = (),
    adjudicator: ReferenceAdjudicatorV3 | None = None,
    claim_coverage: ClaimCitationCoverageV3 | None = None,
) -> CalibrationReferenceLabelsV3:
    case = DevelopmentCalibrationCaseV3.model_validate(case.model_dump(mode="json"))
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.DEVELOPMENT,
        "calibration_id": case.calibration_id,
        "development_observation_id": development_observation_id,
        "development_case_sha256": case.case_sha256,
        "answer_sha256": blinded_answer_sha256_v3(case.answer),
        "source_classification": source_classification,
        "reviewer_ids": tuple(reviewer_ids),
        "scores": dict(scores),
    }
    if adjudicator is not None:
        payload["adjudicator"] = adjudicator.model_dump(mode="json")
    if claim_coverage is not None:
        validate_claim_citation_coverage_v3(case.answer, claim_coverage)
        payload["claim_coverage"] = claim_coverage.model_dump(mode="json")
    return CalibrationReferenceLabelsV3(
        calibration_id=case.calibration_id,
        development_observation_id=development_observation_id,
        development_case_sha256=case.case_sha256,
        answer_sha256=blinded_answer_sha256_v3(case.answer),
        source_classification=source_classification,
        reviewer_ids=tuple(reviewer_ids),
        adjudicator=adjudicator,
        claim_coverage=claim_coverage,
        scores=dict(scores),
        reference_sha256=canonical_sha256(payload),
    )


class CalibrationReferenceBundleV3(FrozenJudgeContractV3):
    schema_version: Literal["3.0"] = "3.0"
    split: Literal[EvaluationSplitV3.DEVELOPMENT] = EvaluationSplitV3.DEVELOPMENT
    protocol_sha256: str = Field(pattern=_SHA256)
    references: tuple[CalibrationReferenceLabelsV3, ...] = Field(min_length=1)
    reference_bundle_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_bundle(self) -> CalibrationReferenceBundleV3:
        for field in (
            "calibration_id",
            "development_observation_id",
            "development_case_sha256",
            "answer_sha256",
        ):
            values = [getattr(item, field) for item in self.references]
            if len(values) != len(set(values)):
                raise ValueError(f"calibration references contain duplicate {field}")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"reference_bundle_sha256"})
        )
        if self.reference_bundle_sha256 != expected_hash:
            raise ValueError("calibration reference bundle canonical hash mismatch")
        return self


def build_calibration_reference_bundle_v3(
    *,
    protocol_sha256: str,
    references: Sequence[CalibrationReferenceLabelsV3],
) -> CalibrationReferenceBundleV3:
    normalized = tuple(
        CalibrationReferenceLabelsV3.model_validate(item.model_dump(mode="json"))
        for item in references
    )
    normalized = tuple(sorted(normalized, key=lambda item: item.calibration_id))
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.DEVELOPMENT,
        "protocol_sha256": protocol_sha256,
        "references": [item.model_dump(mode="json") for item in normalized],
    }
    return CalibrationReferenceBundleV3(
        protocol_sha256=protocol_sha256,
        references=normalized,
        reference_bundle_sha256=canonical_sha256(payload),
    )


class ModelJudgeRequestV3(FrozenJudgeContractV3):
    schema_version: Literal["3.0"] = "3.0"
    phase: Literal["development_calibration", "held_out_scoring"]
    judge_prompt: str
    output_schema_sha256: str = Field(pattern=_SHA256)
    configuration_sha256: str = Field(pattern=_SHA256)
    calibration_sha256: str | None = Field(default=None, pattern=_SHA256)
    answer: BlindedAnswerV3


class CalibrationRecordV3(FrozenJudgeContractV3):
    schema_version: Literal["3.0"] = "3.0"
    split: Literal[EvaluationSplitV3.DEVELOPMENT] = EvaluationSplitV3.DEVELOPMENT
    calibration_id: str = Field(pattern=_IDENTIFIER)
    development_case_sha256: str = Field(pattern=_SHA256)
    protocol_sha256: str = Field(pattern=_SHA256)
    configuration_sha256: str = Field(pattern=_SHA256)
    thresholds_sha256: str = Field(pattern=_SHA256)
    reference_sha256: str = Field(pattern=_SHA256)
    model_output_sha256: str = Field(pattern=_SHA256)
    reference_scores: dict[EvaluationMetricV3, float]
    observed_scores: dict[EvaluationMetricV3, float]
    absolute_errors: dict[EvaluationMetricV3, float]
    reference_claim_coverage: ClaimCitationCoverageV3 | None = None
    observed_claim_coverage: ClaimCitationCoverageV3 | None = None
    claim_disagreement_count: int | None = Field(default=None, ge=0)
    validated_judge_output: dict[str, Any] | None = None
    development_case: DevelopmentCalibrationCaseV3 | None = None
    record_sha256: str = Field(pattern=_SHA256)

    @model_serializer(mode="wrap")
    def preserve_legacy_record(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        payload = handler(self)
        for name in (
            "reference_claim_coverage",
            "observed_claim_coverage",
            "claim_disagreement_count",
            "validated_judge_output",
            "development_case",
        ):
            if payload.get(name) is None:
                payload.pop(name, None)
        return payload

    @model_validator(mode="after")
    def validate_record(self) -> CalibrationRecordV3:
        expected_metrics = set(SEMANTIC_JUDGE_METRICS_V3)
        if any(
            set(values) != expected_metrics
            for values in (
                self.reference_scores,
                self.observed_scores,
                self.absolute_errors,
            )
        ):
            raise ValueError("calibration scores must cover every semantic metric")
        if any(
            value < 0 or value > 1
            for values in (self.reference_scores, self.observed_scores)
            for value in values.values()
        ):
            raise ValueError("calibration scores must be between zero and one")
        for metric in SEMANTIC_JUDGE_METRICS_V3:
            expected_error = abs(
                self.reference_scores[metric] - self.observed_scores[metric]
            )
            if self.absolute_errors[metric] != expected_error:
                raise ValueError("calibration absolute error mismatch")
        extensions = (
            self.reference_claim_coverage,
            self.observed_claim_coverage,
            self.claim_disagreement_count,
            self.validated_judge_output,
            self.development_case,
        )
        if any(value is not None for value in extensions):
            if any(value is None for value in extensions):
                raise ValueError("per-claim calibration evidence is incomplete")
            reference, observed = (
                self.reference_claim_coverage,
                self.observed_claim_coverage,
            )
            assert reference is not None and observed is not None
            assert self.development_case is not None
            if (
                self.development_case.calibration_id != self.calibration_id
                or self.development_case.case_sha256 != self.development_case_sha256
                or canonical_sha256(self.development_case.answer)
                != reference.answer_sha256
            ):
                raise ValueError("calibration native case differs from record binding")
            if (reference.answer_sha256, reference.claim_count) != (
                observed.answer_sha256,
                observed.claim_count,
            ):
                raise ValueError("per-claim calibration answers or denominators differ")
            disagreements = sum(
                expected.supported != actual.supported
                for expected, actual in zip(
                    reference.verdicts, observed.verdicts, strict=True
                )
            )
            if self.claim_disagreement_count != disagreements:
                raise ValueError("per-claim calibration disagreement count differs")
            output = ClaimCoverageModelJudgeOutputV3.model_validate(
                self.validated_judge_output
            )
            if canonical_sha256(output) != self.model_output_sha256 or (
                output.claim_verdicts != observed.verdicts
                or self.observed_scores
                != {item.metric: item.score for item in output.verdicts}
            ):
                raise ValueError(
                    "per-claim calibration differs from validated model output"
                )
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"record_sha256"})
        )
        if self.record_sha256 != expected_hash:
            raise ValueError("calibration record canonical hash mismatch")
        return self


class CalibrationThresholdsV3(FrozenJudgeContractV3):
    minimum_development_cases: int = Field(ge=1)
    maximum_absolute_error: dict[EvaluationMetricV3, float]
    thresholds_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_thresholds(self) -> CalibrationThresholdsV3:
        if set(self.maximum_absolute_error) != set(SEMANTIC_JUDGE_METRICS_V3):
            raise ValueError("calibration thresholds must cover every semantic metric")
        if any(
            value < 0 or value > 1 for value in self.maximum_absolute_error.values()
        ):
            raise ValueError("calibration thresholds must be between zero and one")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"thresholds_sha256"})
        )
        if self.thresholds_sha256 != expected_hash:
            raise ValueError("calibration thresholds canonical hash mismatch")
        return self


def build_calibration_thresholds_v3(
    *,
    minimum_development_cases: int,
    maximum_absolute_error: Mapping[EvaluationMetricV3, float],
) -> CalibrationThresholdsV3:
    values = dict(maximum_absolute_error)
    payload = {
        "minimum_development_cases": minimum_development_cases,
        "maximum_absolute_error": values,
    }
    return CalibrationThresholdsV3(
        minimum_development_cases=minimum_development_cases,
        maximum_absolute_error=values,
        thresholds_sha256=canonical_sha256(payload),
    )


class CalibrationFreezeV3(FrozenJudgeContractV3):
    schema_version: Literal["3.0"] = "3.0"
    protocol_sha256: str = Field(pattern=_SHA256)
    configuration_sha256: str = Field(pattern=_SHA256)
    thresholds_sha256: str = Field(pattern=_SHA256)
    reference_bundle_sha256: str = Field(pattern=_SHA256)
    expected_calibration_ids: tuple[str, ...] = Field(min_length=1)
    development_record_sha256s: tuple[str, ...] = Field(min_length=1)
    maximum_observed_errors: dict[EvaluationMetricV3, float]
    claim_calibration_claim_count: int | None = Field(default=None, ge=1)
    claim_calibration_disagreement_count: int | None = Field(default=None, ge=0)
    claim_calibration_error: float | None = Field(default=None, ge=0, le=1)
    claim_calibration_error_threshold: float | None = Field(default=None, ge=0, le=1)
    calibration_sha256: str = Field(pattern=_SHA256)

    @model_serializer(mode="wrap")
    def preserve_legacy_freeze(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        payload = handler(self)
        for name in (
            "claim_calibration_claim_count",
            "claim_calibration_disagreement_count",
            "claim_calibration_error",
            "claim_calibration_error_threshold",
        ):
            if payload.get(name) is None:
                payload.pop(name, None)
        return payload

    @model_validator(mode="after")
    def validate_freeze(self) -> CalibrationFreezeV3:
        if len(self.development_record_sha256s) != len(
            set(self.development_record_sha256s)
        ):
            raise ValueError("calibration freeze contains duplicate records")
        if tuple(sorted(set(self.expected_calibration_ids))) != (
            self.expected_calibration_ids
        ):
            raise ValueError("expected calibration IDs must be sorted and unique")
        if len(self.expected_calibration_ids) != len(self.development_record_sha256s):
            raise ValueError("calibration freeze record set is incomplete")
        if set(self.maximum_observed_errors) != set(SEMANTIC_JUDGE_METRICS_V3):
            raise ValueError("calibration freeze metric set is incomplete")
        extensions = (
            self.claim_calibration_claim_count,
            self.claim_calibration_disagreement_count,
            self.claim_calibration_error,
            self.claim_calibration_error_threshold,
        )
        if any(value is not None for value in extensions):
            if any(value is None for value in extensions):
                raise ValueError("per-claim calibration freeze is incomplete")
            assert self.claim_calibration_claim_count is not None
            assert self.claim_calibration_disagreement_count is not None
            assert self.claim_calibration_error is not None
            assert self.claim_calibration_error_threshold is not None
            if (
                self.claim_calibration_error
                != (
                    self.claim_calibration_disagreement_count
                    / self.claim_calibration_claim_count
                )
                or self.claim_calibration_error > self.claim_calibration_error_threshold
            ):
                raise ValueError("per-claim calibration error exceeds frozen threshold")
        expected_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"calibration_sha256"})
        )
        if self.calibration_sha256 != expected_hash:
            raise ValueError("calibration freeze canonical hash mismatch")
        return self


JudgeCallableV3 = Callable[[ModelJudgeRequestV3], object]


def run_development_calibration_case_v3(
    configuration: ModelJudgeConfigurationV3,
    case: DevelopmentCalibrationCaseV3,
    *,
    reference: CalibrationReferenceLabelsV3,
    thresholds: CalibrationThresholdsV3,
    judge: JudgeCallableV3,
) -> CalibrationRecordV3:
    configuration = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    case = DevelopmentCalibrationCaseV3.model_validate(case.model_dump(mode="json"))
    reference = CalibrationReferenceLabelsV3.model_validate(
        reference.model_dump(mode="json")
    )
    thresholds = CalibrationThresholdsV3.model_validate(
        thresholds.model_dump(mode="json")
    )
    if reference.calibration_id != case.calibration_id:
        raise ValueError("reference labels use a different calibration ID")
    if reference.development_case_sha256 != case.case_sha256:
        raise ValueError("reference labels use a different development case")
    if reference.answer_sha256 != blinded_answer_sha256_v3(case.answer):
        raise ValueError("reference labels use a different blinded answer")
    validate_calibration_reference_v3(configuration, case, reference)
    output = _invoke_and_validate(
        configuration,
        case.answer,
        phase="development_calibration",
        calibration_sha256=None,
        judge=judge,
    )
    return build_calibration_record_v3(
        configuration=configuration,
        case=case,
        reference=reference,
        thresholds=thresholds,
        output=output,
    )


def build_calibration_record_v3(
    *,
    configuration: ModelJudgeConfigurationV3,
    case: DevelopmentCalibrationCaseV3,
    reference: CalibrationReferenceLabelsV3,
    thresholds: CalibrationThresholdsV3,
    output: ModelJudgeOutputV3,
) -> CalibrationRecordV3:
    validate_calibration_reference_v3(configuration, case, reference)
    output = validate_model_judge_output_v3(output, case.answer)
    if (
        reference.calibration_id,
        reference.development_case_sha256,
        reference.answer_sha256,
    ) != (case.calibration_id, case.case_sha256, canonical_sha256(case.answer)):
        raise ValueError("reference labels use a different development answer")
    references = dict(reference.scores)
    observed: dict[EvaluationMetricV3, float] = {
        item.metric: item.score for item in output.verdicts
    }
    errors = {
        metric: abs(references[metric] - observed[metric])
        for metric in SEMANTIC_JUDGE_METRICS_V3
    }
    payload: dict[str, Any] = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.DEVELOPMENT,
        "calibration_id": case.calibration_id,
        "development_case_sha256": case.case_sha256,
        "protocol_sha256": configuration.bindings.protocol_sha256,
        "configuration_sha256": configuration.configuration_sha256,
        "thresholds_sha256": thresholds.thresholds_sha256,
        "reference_sha256": reference.reference_sha256,
        "model_output_sha256": canonical_sha256(output),
        "reference_scores": references,
        "observed_scores": observed,
        "absolute_errors": errors,
    }
    if isinstance(output, ClaimCoverageModelJudgeOutputV3):
        assert reference.claim_coverage is not None
        observed_coverage = claim_citation_coverage_from_output_v3(case.answer, output)
        assert observed_coverage is not None
        payload.update(
            {
                "reference_claim_coverage": reference.claim_coverage.model_dump(
                    mode="json"
                ),
                "observed_claim_coverage": observed_coverage.model_dump(mode="json"),
                "claim_disagreement_count": sum(
                    expected.supported != actual.supported
                    for expected, actual in zip(
                        reference.claim_coverage.verdicts,
                        observed_coverage.verdicts,
                        strict=True,
                    )
                ),
                "validated_judge_output": output.model_dump(mode="json"),
                "development_case": case.model_dump(mode="json"),
            }
        )
    return CalibrationRecordV3(
        **payload,
        record_sha256=canonical_sha256(payload),
    )


def validate_reference_claim_coverage_v3(
    case: DevelopmentCalibrationCaseV3, reference: CalibrationReferenceLabelsV3
) -> None:
    if reference.claim_coverage is None or reference.adjudicator is None:
        raise ValueError("successor calibration requires independent per-claim labels")
    if reference.source_classification == "automated_gold_derived":
        raise ValueError("per-claim calibration cannot use gold-derived labels")
    validate_claim_citation_coverage_v3(case.answer, reference.claim_coverage)
    support = reference.scores[EvaluationMetricV3.CLAIM_SUPPORT]
    expected_scores = {**score_runtime_metrics_v3(case.answer)}
    expected_scores[EvaluationMetricV3.CLAIM_SUPPORT] = support
    expected_scores[EvaluationMetricV3.TASK_COMPLETION] = float(
        all(score == 1.0 for score in expected_scores.values())
    )
    if support not in (0.0, 1.0) or reference.scores != expected_scores:
        raise ValueError("independent reference differs from runtime or binary labels")
    if support == 1.0 and (
        any(not verdict.supported for verdict in reference.claim_coverage.verdicts)
        or {
            index
            for verdict in reference.claim_coverage.verdicts
            for index in verdict.rubric_fact_indices
        }
        != set(range(len(case.answer.rubric_context.required_facts)))
    ):
        raise ValueError("positive reference support differs from per-claim labels")


def validate_calibration_reference_v3(
    configuration: ModelJudgeConfigurationV3,
    case: DevelopmentCalibrationCaseV3,
    reference: CalibrationReferenceLabelsV3,
) -> None:
    if (
        reference.calibration_id,
        reference.development_case_sha256,
        reference.answer_sha256,
    ) != (case.calibration_id, case.case_sha256, canonical_sha256(case.answer)):
        raise ValueError("reference labels use a different development answer")
    successor = configuration.bindings.judge_schema_sha256 == (
        model_judge_output_schema_sha256_v3(successor=True)
    )
    if successor != (case.answer.rubric_context.evaluator_contract is not None):
        raise ValueError("development answer differs from configured judge schema")
    if successor:
        validate_reference_claim_coverage_v3(case, reference)
        assert reference.adjudicator is not None
        if reference.adjudicator.model_snapshot == configuration.model_binding.model:
            raise ValueError("reference adjudicator must differ from the scored judge")


def freeze_calibration_thresholds_v3(
    configuration: ModelJudgeConfigurationV3,
    records: Sequence[CalibrationRecordV3],
    thresholds: CalibrationThresholdsV3,
    reference_bundle: CalibrationReferenceBundleV3,
    *,
    expected_calibration_ids: Sequence[str],
    development_cases: Sequence[DevelopmentCalibrationCaseV3] | None = None,
) -> CalibrationFreezeV3:
    configuration = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    records = tuple(
        CalibrationRecordV3.model_validate(item.model_dump(mode="json"))
        for item in records
    )
    thresholds = CalibrationThresholdsV3.model_validate(
        thresholds.model_dump(mode="json")
    )
    reference_bundle = CalibrationReferenceBundleV3.model_validate(
        reference_bundle.model_dump(mode="json")
    )
    expected_ids = tuple(sorted(expected_calibration_ids))
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("expected calibration IDs must be unique")
    if reference_bundle.protocol_sha256 != configuration.bindings.protocol_sha256:
        raise ValueError("calibration references use a different benchmark protocol")
    reference_by_id = {
        item.calibration_id: item for item in reference_bundle.references
    }
    if set(reference_by_id) != set(expected_ids):
        raise ValueError(
            "calibration reference set differs from expected development set"
        )
    if len(records) < thresholds.minimum_development_cases:
        raise ValueError("insufficient development calibration records")
    if len({item.calibration_id for item in records}) != len(records):
        raise ValueError("development calibration IDs must be unique")
    if {item.calibration_id for item in records} != set(expected_ids):
        raise ValueError("development calibration record set is incomplete")
    if any(
        item.protocol_sha256 != configuration.bindings.protocol_sha256
        or item.configuration_sha256 != configuration.configuration_sha256
        or item.thresholds_sha256 != thresholds.thresholds_sha256
        or item.reference_sha256
        != reference_by_id[item.calibration_id].reference_sha256
        or item.development_case_sha256
        != reference_by_id[item.calibration_id].development_case_sha256
        for item in records
    ):
        raise ValueError("calibration record provenance drift detected")
    maxima = {
        metric: max(item.absolute_errors[metric] for item in records)
        for metric in SEMANTIC_JUDGE_METRICS_V3
    }
    failed = [
        metric.value
        for metric in SEMANTIC_JUDGE_METRICS_V3
        if maxima[metric] > thresholds.maximum_absolute_error[metric]
    ]
    if failed:
        raise ValueError(f"development calibration exceeds frozen thresholds: {failed}")
    record_hashes = tuple(sorted(item.record_sha256 for item in records))
    payload: dict[str, Any] = {
        "schema_version": "3.0",
        "protocol_sha256": configuration.bindings.protocol_sha256,
        "configuration_sha256": configuration.configuration_sha256,
        "thresholds_sha256": thresholds.thresholds_sha256,
        "reference_bundle_sha256": reference_bundle.reference_bundle_sha256,
        "expected_calibration_ids": expected_ids,
        "development_record_sha256s": record_hashes,
        "maximum_observed_errors": maxima,
    }
    if (
        configuration.bindings.judge_schema_sha256
        == model_judge_output_schema_sha256_v3(successor=True)
    ):
        cases = tuple(
            DevelopmentCalibrationCaseV3.model_validate(case.model_dump(mode="json"))
            for case in (
                development_cases
                if development_cases is not None
                else tuple(
                    item.development_case
                    for item in records
                    if item.development_case is not None
                )
            )
        )
        case_by_id = {case.calibration_id: case for case in cases}
        if len(case_by_id) != len(cases) or set(case_by_id) != set(expected_ids):
            raise ValueError(
                "successor freeze lacks actual development case authorities"
            )
        for record in records:
            reference = reference_by_id[record.calibration_id]
            if (
                reference.claim_coverage is None
                or reference.adjudicator is None
                or reference.source_classification == "automated_gold_derived"
                or reference.adjudicator.model_snapshot
                == configuration.model_binding.model
                or record.reference_claim_coverage != reference.claim_coverage
                or record.observed_claim_coverage is None
                or record.claim_disagreement_count is None
                or record.validated_judge_output is None
                or record.reference_scores != reference.scores
            ):
                raise ValueError(
                    "successor freeze lacks independent per-claim calibration evidence"
                )
            rebuilt = build_calibration_record_v3(
                configuration=configuration,
                case=case_by_id[record.calibration_id],
                reference=reference,
                thresholds=thresholds,
                output=validate_model_judge_output_v3(
                    record.validated_judge_output,
                    case_by_id[record.calibration_id].answer,
                ),
            )
            if rebuilt != record:
                raise ValueError(
                    "calibration record differs from actual development case"
                )
        claim_count = sum(
            item.reference_claim_coverage.claim_count
            for item in records
            if item.reference_claim_coverage is not None
        )
        if not claim_count:
            raise ValueError("successor calibration has no actual development claims")
        disagreement_count = sum(item.claim_disagreement_count or 0 for item in records)
        error = disagreement_count / claim_count
        threshold = thresholds.maximum_absolute_error[EvaluationMetricV3.CLAIM_SUPPORT]
        if error > threshold:
            raise ValueError(
                "per-claim calibration exceeds frozen claim-support threshold"
            )
        payload.update(
            {
                "claim_calibration_claim_count": claim_count,
                "claim_calibration_disagreement_count": disagreement_count,
                "claim_calibration_error": error,
                "claim_calibration_error_threshold": threshold,
            }
        )
    return CalibrationFreezeV3(
        **payload,
        calibration_sha256=canonical_sha256(payload),
    )


def validate_claim_calibration_freeze_v3(
    configuration: ModelJudgeConfigurationV3, calibration: CalibrationFreezeV3
) -> None:
    calibration = CalibrationFreezeV3.model_validate(
        calibration.model_dump(mode="json")
    )
    successor = configuration.bindings.judge_schema_sha256 == (
        model_judge_output_schema_sha256_v3(successor=True)
    )
    if successor != (calibration.claim_calibration_claim_count is not None):
        raise ValueError("judge schema requires its bound per-claim calibration freeze")


def judge_blinded_packet_v3(
    packet: BlindedAnswerPacketV3,
    configuration: ModelJudgeConfigurationV3,
    calibration: CalibrationFreezeV3,
    *,
    judge: JudgeCallableV3,
) -> tuple[JudgmentRecordV3, ...]:
    """Score held-out answers without exposing the unblinding key to the judge."""

    packet = BlindedAnswerPacketV3.model_validate(packet.model_dump(mode="json"))
    configuration = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    calibration = CalibrationFreezeV3.model_validate(
        calibration.model_dump(mode="json")
    )
    if packet.split != EvaluationSplitV3.HELD_OUT:
        raise ValueError("semantic scoring requires a held-out blind packet")
    if packet.bindings != configuration.bindings:
        raise ValueError("blind packet and judge configuration bindings differ")
    if calibration.protocol_sha256 != packet.bindings.protocol_sha256:
        raise ValueError("calibration freeze uses a different benchmark protocol")
    if calibration.configuration_sha256 != configuration.configuration_sha256:
        raise ValueError("calibration freeze uses a different judge configuration")
    validate_claim_calibration_freeze_v3(configuration, calibration)

    judgments: list[JudgmentRecordV3] = []
    for answer in packet.answers:
        output = _invoke_and_validate(
            configuration,
            answer,
            phase="held_out_scoring",
            calibration_sha256=calibration.calibration_sha256,
            judge=judge,
        )
        model_scores: dict[EvaluationMetricV3, float] = {
            item.metric: item.score for item in output.verdicts
        }
        claim_coverage = claim_citation_coverage_from_output_v3(answer, output)
        deterministic_scores = score_deterministic_metrics_v3(
            answer, claim_coverage=claim_coverage
        )
        scores: dict[EvaluationMetricV3, float | None] = {
            **model_scores,
            **deterministic_scores,
        }
        score_sources: dict[
            EvaluationMetricV3,
            Literal["deterministic", "model_judge", "human_review"],
        ] = {
            **{metric: "model_judge" for metric in model_scores},
            **{metric: "deterministic" for metric in deterministic_scores},
        }
        if claim_coverage is not None:
            score_sources[EvaluationMetricV3.CITATION_COVERAGE] = "model_judge"
        judgments.append(
            build_judgment_record_v3(
                bindings=packet.bindings,
                blinded_packet_sha256=packet.packet_sha256,
                opaque_answer_id=answer.opaque_answer_id,
                judgment_mode=JudgmentModeV3.MODEL_JUDGE,
                model_binding=configuration.model_binding,
                scores=scores,
                score_sources=score_sources,
                judge_configuration_sha256=configuration.configuration_sha256,
                calibration_sha256=calibration.calibration_sha256,
                judge_output_sha256=canonical_sha256(output),
                claim_coverage=claim_coverage,
                validated_judge_output=(
                    output.model_dump(mode="json")
                    if claim_coverage is not None
                    else None
                ),
            )
        )
    return tuple(judgments)


def score_deterministic_metrics_v3(
    answer: BlindedAnswerV3,
    *,
    claim_coverage: ClaimCitationCoverageV3 | None = None,
) -> dict[EvaluationMetricV3, float | None]:
    """Keep legacy freezes readable; score successor facts by record/field binding."""

    if answer.rubric_context.evaluator_contract == "evidence_semantics_v2":
        facts = answer.rubric_context.required_facts
        supported = [
            any(citation_supports_fact_v3(citation, fact) for fact in facts)
            for citation in answer.citations
        ]
        if claim_coverage is None:
            raise ValueError("successor citation coverage requires per-claim verdicts")
        validate_claim_citation_coverage_v3(answer, claim_coverage)
        return {
            EvaluationMetricV3.CITATION_PRECISION: sum(supported) / len(supported)
            if supported
            else float(not facts),
            # Semantic per-claim decisions are model-judge authority; only their
            # aggregation is deterministic. Required-fact recall is not coverage.
            EvaluationMetricV3.CITATION_COVERAGE: claim_coverage.coverage,
            # Fact support blueprints are not document-relevance annotations, and
            # final citations are not the set of retrieved records.
            EvaluationMetricV3.DOCUMENT_RECALL: None,
        }

    expected_evidence = {item.evidence for item in answer.rubric_context.required_facts}
    cited_evidence = [item.evidence for item in answer.citations]
    supported_citations = [item for item in cited_evidence if item in expected_evidence]
    precision = (
        len(supported_citations) / len(cited_evidence)
        if cited_evidence
        else float(not expected_evidence)
    )
    recalled = len(set(supported_citations))
    recall = recalled / len(expected_evidence) if expected_evidence else 1.0
    return {
        EvaluationMetricV3.CITATION_PRECISION: precision,
        EvaluationMetricV3.CITATION_COVERAGE: recall,
        EvaluationMetricV3.DOCUMENT_RECALL: recall,
    }


def validate_claim_citation_coverage_v3(
    answer: BlindedAnswerV3, coverage: ClaimCitationCoverageV3
) -> None:
    coverage = ClaimCitationCoverageV3.model_validate(coverage.model_dump(mode="json"))
    if coverage.answer_sha256 != canonical_sha256(answer):
        raise ValueError("claim coverage references a different receipt-bound answer")
    if coverage.claim_count != len(answer.claims):
        raise ValueError("claim coverage denominator differs from receipt claims")
    citations = {item.label: item for item in answer.citations}
    facts = answer.rubric_context.required_facts
    for verdict, claim in zip(coverage.verdicts, answer.claims, strict=True):
        if not set(verdict.citation_labels) <= set(citations):
            raise ValueError("claim verdict references an unknown citation")
        if not set(verdict.citation_labels) <= set(claim.citation_labels):
            raise ValueError("claim verdict borrows another claim's cited evidence")
        if verdict.supported and not verdict.citation_labels:
            raise ValueError("supported claim lacks its own cited evidence")
        if verdict.supported and any(
            not citations[label].source_id or not citations[label].source_version_id
            for label in verdict.citation_labels
        ):
            raise ValueError("supported claim lacks immutable citation authority")
        if any(index >= len(facts) for index in verdict.rubric_fact_indices):
            raise ValueError("claim verdict references an unknown rubric fact")
        if not verdict.supported:
            continue
        for index in verdict.rubric_fact_indices:
            fact = facts[index]
            if not any(
                citation_supports_fact_v3(citations[label], fact)
                for label in verdict.citation_labels
            ):
                raise ValueError("supported claim lacks bound fact evidence")
            if fact.match_mode != "fact_semantics" and not exact_value_in_text_v3(
                fact.expected_value,
                claim.text,
                numeric=fact.match_mode == "numeric_exact",
            ):
                raise ValueError("supported claim violates exact fact matching")


def claim_citation_coverage_from_output_v3(
    answer: BlindedAnswerV3, output: ModelJudgeOutputV3
) -> ClaimCitationCoverageV3 | None:
    if answer.rubric_context.evaluator_contract is None:
        return None
    if not isinstance(output, ClaimCoverageModelJudgeOutputV3):
        raise ValueError("successor judge output requires per-claim semantic verdicts")
    return build_claim_citation_coverage_v3(answer, output.claim_verdicts)


def build_claim_citation_coverage_v3(
    answer: BlindedAnswerV3, verdicts: Sequence[ClaimCitationVerdictV3]
) -> ClaimCitationCoverageV3:
    count = len(answer.claims)
    supported = sum(item.supported for item in verdicts)
    coverage = ClaimCitationCoverageV3(
        answer_sha256=canonical_sha256(answer),
        claim_count=count,
        supported_claim_count=supported,
        verdicts=tuple(verdicts),
        coverage=supported / count if count else None,
    )
    validate_claim_citation_coverage_v3(answer, coverage)
    return coverage


def validate_judgment_claim_coverage_v3(
    judgment: JudgmentRecordV3, answer: BlindedAnswerV3
) -> None:
    successor = answer.rubric_context.evaluator_contract is not None
    if successor != (judgment.claim_coverage is not None):
        raise ValueError("judgment claim coverage contract differs from blind answer")
    if successor != (
        judgment.bindings.judge_schema_sha256
        == model_judge_output_schema_sha256_v3(successor=True)
    ):
        raise ValueError("judgment claim coverage schema binding differs from answer")
    if judgment.claim_coverage is None:
        return
    validate_claim_citation_coverage_v3(answer, judgment.claim_coverage)
    source = (
        "human_review"
        if judgment.judgment_mode.value == "human_review"
        else "model_judge"
    )
    if judgment.score_sources.get(EvaluationMetricV3.CITATION_COVERAGE) != source:
        raise ValueError("claim coverage score source differs from semantic authority")
    if (
        judgment.scores.get(EvaluationMetricV3.CITATION_COVERAGE)
        != judgment.claim_coverage.coverage
    ):
        raise ValueError("judgment citation coverage differs from bound claim verdicts")
    if judgment.scores.get(EvaluationMetricV3.CLAIM_SUPPORT) == 1.0 and any(
        not item.supported for item in judgment.claim_coverage.verdicts
    ):
        raise ValueError("positive claim support conflicts with per-claim verdicts")
    if judgment.judgment_mode.value == "model_judge":
        if judgment.validated_judge_output is None:
            raise ValueError(
                "successor judgment lacks validated per-claim judge output"
            )
        output = validate_model_judge_output_v3(judgment.validated_judge_output, answer)
        if canonical_sha256(output) != judgment.judge_output_sha256:
            raise ValueError("claim verdicts differ from bound judge output hash")
        observed_coverage = claim_citation_coverage_from_output_v3(answer, output)
        if observed_coverage != judgment.claim_coverage:
            raise ValueError("claim coverage differs from validated judge output")
        expected_scores: dict[EvaluationMetricV3, float | None] = {
            **{item.metric: item.score for item in output.verdicts},
            **score_deterministic_metrics_v3(answer, claim_coverage=observed_coverage),
        }
        if judgment.scores != expected_scores:
            raise ValueError("judgment scores differ from validated judge output")


def score_runtime_metrics_v3(
    answer: BlindedAnswerV3,
) -> dict[EvaluationMetricV3, float]:
    """Exact successor predicates over receipt-bound outcome and plan evidence."""
    runtime, rubric = answer.runtime_evidence, answer.rubric_context
    successful = set(runtime.successful_capabilities)
    allowed, forbidden = (
        set(rubric.allowed_capabilities),
        set(rubric.forbidden_capabilities),
    )
    authorized = successful <= allowed and not successful.intersection(forbidden)
    valid_plan = (
        runtime.plan_revision_count > 0
        and set(runtime.planned_capabilities) <= allowed | forbidden
        and authorized
    )
    required = set(rubric.required_capabilities)
    valid_plan = valid_plan and (
        not successful.intersection(required)
        if rubric.expected_action_outcome == "denied"
        else required <= successful
    )
    answered = runtime.outcome == rubric.expected_dialogue_outcome
    useful = answered and (
        runtime.plan_revision_count == 1
        or (
            runtime.plan_revision_count == 2
            and bool(runtime.final_revision_added_read_step_ids)
            and set(runtime.final_revision_added_read_step_ids)
            <= set(runtime.successful_step_ids)
            | set(runtime.final_revision_reused_step_ids)
        )
    )
    return {
        EvaluationMetricV3.ANSWERABILITY_ABSTENTION: float(answered),
        EvaluationMetricV3.AUTHORIZATION: float(authorized),
        EvaluationMetricV3.VALID_PLAN: float(valid_plan),
        EvaluationMetricV3.USEFUL_CONTINUATION: float(useful),
    }


def validate_model_judge_output_v3(
    raw_output: object,
    answer: BlindedAnswerV3,
) -> ModelJudgeOutputV3:
    output_type = (
        ClaimCoverageModelJudgeOutputV3
        if answer.rubric_context.evaluator_contract is not None
        else ModelJudgeOutputV3
    )
    if isinstance(raw_output, (str, bytes, bytearray)):
        output = output_type.model_validate_json(raw_output)
    elif isinstance(raw_output, ModelJudgeOutputV3):
        output = output_type.model_validate(raw_output.model_dump(mode="json"))
    else:
        output = output_type.model_validate(raw_output)
    if output.opaque_answer_id != answer.opaque_answer_id:
        raise ValueError("model judgment references a different blinded answer")

    citation_by_label = {item.label: item for item in answer.citations}
    if len(citation_by_label) != len(answer.citations):
        raise ValueError("blinded answer contains duplicate citation labels")
    fact_count = len(answer.rubric_context.required_facts)
    for verdict in output.verdicts:
        if any(index >= fact_count for index in verdict.rubric_fact_indices):
            raise ValueError("model judgment references an unknown rubric fact")
        unknown_labels = sorted(set(verdict.citation_labels) - set(citation_by_label))
        if unknown_labels:
            raise ValueError(
                f"model judgment contains fabricated citations: {unknown_labels}"
            )
    if answer.rubric_context.evaluator_contract == "evidence_semantics_v2":
        coverage = claim_citation_coverage_from_output_v3(answer, output)
        assert coverage is not None
        scores: dict[EvaluationMetricV3, float] = {
            item.metric: item.score for item in output.verdicts
        }
        if any(
            scores[metric] != expected
            for metric, expected in score_runtime_metrics_v3(answer).items()
        ):
            raise ValueError(
                "model verdict differs from receipt-bound runtime predicates"
            )
        if scores[EvaluationMetricV3.TASK_COMPLETION] != float(
            all(
                scores[metric] == 1.0
                for metric in SEMANTIC_JUDGE_METRICS_V3
                if metric is not EvaluationMetricV3.TASK_COMPLETION
            )
        ):
            raise ValueError("task completion differs from component verdicts")
        support = next(
            item
            for item in output.verdicts
            if item.metric is EvaluationMetricV3.CLAIM_SUPPORT
        )
        if support.score == 1.0:
            if any(not verdict.supported for verdict in coverage.verdicts):
                raise ValueError(
                    "positive claim support conflicts with per-claim verdicts"
                )
            if {
                index
                for verdict in coverage.verdicts
                for index in verdict.rubric_fact_indices
            } != set(range(fact_count)):
                raise ValueError(
                    "positive claim support lacks per-claim required facts"
                )
            if set(support.rubric_fact_indices) != set(range(fact_count)):
                raise ValueError(
                    "positive claim support must reference every required fact"
                )
            for claim in answer.claims:
                if not set(claim.citation_labels).intersection(support.citation_labels):
                    raise ValueError(
                        "positive claim support lacks a claim's own cited evidence"
                    )
            for fact in answer.rubric_context.required_facts:
                supported_claims = [
                    claim
                    for claim in answer.claims
                    if any(
                        label in support.citation_labels
                        and citation_supports_fact_v3(citation_by_label[label], fact)
                        for label in claim.citation_labels
                    )
                ]
                if not supported_claims:
                    raise ValueError("positive claim support lacks bound fact evidence")
                if fact.match_mode != "fact_semantics" and not any(
                    exact_value_in_text_v3(
                        fact.expected_value,
                        claim.text,
                        numeric=fact.match_mode == "numeric_exact",
                    )
                    for claim in supported_claims
                ):
                    raise ValueError(
                        "positive claim support violates exact fact matching"
                    )
    return output


def _validate_complete_score_map(
    scores: Mapping[EvaluationMetricV3, float],
    *,
    label: str,
) -> None:
    if set(scores) != set(SEMANTIC_JUDGE_METRICS_V3):
        raise ValueError(f"{label} scores must cover every semantic metric")
    if any(value < 0 or value > 1 for value in scores.values()):
        raise ValueError(f"{label} scores must be between zero and one")


def _invoke_and_validate(
    configuration: ModelJudgeConfigurationV3,
    answer: BlindedAnswerV3,
    *,
    phase: Literal["development_calibration", "held_out_scoring"],
    calibration_sha256: str | None,
    judge: JudgeCallableV3,
) -> ModelJudgeOutputV3:
    request = ModelJudgeRequestV3(
        phase=phase,
        judge_prompt=configuration.judge_prompt,
        output_schema_sha256=configuration.bindings.judge_schema_sha256,
        configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=calibration_sha256,
        answer=answer,
    )
    return validate_model_judge_output_v3(judge(request), answer)


# Readable alias for callers that treat the judge as the semantic scoring stage.
score_blinded_packet_v3 = judge_blinded_packet_v3
