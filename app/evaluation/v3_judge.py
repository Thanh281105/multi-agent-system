"""Calibrated, blinded semantic-judge boundary for Evaluation v3.

The provider boundary in this module accepts only a blinded answer and immutable
judge metadata.  Development calibration is represented separately and must be
frozen before a held-out packet can be scored.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerPacketV3,
    BlindedAnswerV3,
    JudgmentRecordV3,
    build_judgment_record_v3,
)
from app.evaluation.v3_comparison import ArtifactBindingsV3
from app.evaluation.v3_gold import EvaluationSplitV3
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
    metric: EvaluationMetricV3
    score: float = Field(ge=0, le=1)
    rubric_fact_indices: tuple[int, ...] = ()
    citation_labels: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> SemanticMetricVerdictV3:
        if self.metric not in SEMANTIC_JUDGE_METRICS_V3:
            raise ValueError("model output contains a non-semantic metric")
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
        if metrics != SEMANTIC_JUDGE_METRICS_V3:
            raise ValueError("model output must contain every semantic metric in order")
        expected_schema_hash = model_judge_output_schema_sha256_v3()
        if self.output_schema_sha256 != expected_schema_hash:
            raise ValueError("model output schema hash mismatch")
        return self


def model_judge_output_schema_sha256_v3() -> str:
    schema = ModelJudgeOutputV3.model_json_schema()
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
        schema_hash = model_judge_output_schema_sha256_v3()
        if schema_hash != self.bindings.judge_schema_sha256:
            raise ValueError("judge output schema hash mismatch")
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
    configuration_sha256: str = Field(pattern=_SHA256)
    model_output_sha256: str = Field(pattern=_SHA256)
    reference_scores: dict[EvaluationMetricV3, float]
    observed_scores: dict[EvaluationMetricV3, float]
    absolute_errors: dict[EvaluationMetricV3, float]
    record_sha256: str = Field(pattern=_SHA256)

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
    development_record_sha256s: tuple[str, ...] = Field(min_length=1)
    maximum_observed_errors: dict[EvaluationMetricV3, float]
    calibration_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_freeze(self) -> CalibrationFreezeV3:
        if len(self.development_record_sha256s) != len(
            set(self.development_record_sha256s)
        ):
            raise ValueError("calibration freeze contains duplicate records")
        if set(self.maximum_observed_errors) != set(SEMANTIC_JUDGE_METRICS_V3):
            raise ValueError("calibration freeze metric set is incomplete")
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
    reference_scores: Mapping[EvaluationMetricV3, float],
    judge: JudgeCallableV3,
) -> CalibrationRecordV3:
    configuration = ModelJudgeConfigurationV3.model_validate(
        configuration.model_dump(mode="json")
    )
    case = DevelopmentCalibrationCaseV3.model_validate(case.model_dump(mode="json"))
    references = dict(reference_scores)
    _validate_complete_score_map(references, label="reference calibration")
    output = _invoke_and_validate(
        configuration,
        case.answer,
        phase="development_calibration",
        calibration_sha256=None,
        judge=judge,
    )
    observed = {item.metric: item.score for item in output.verdicts}
    errors = {
        metric: abs(references[metric] - observed[metric])
        for metric in SEMANTIC_JUDGE_METRICS_V3
    }
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.DEVELOPMENT,
        "calibration_id": case.calibration_id,
        "development_case_sha256": case.case_sha256,
        "configuration_sha256": configuration.configuration_sha256,
        "model_output_sha256": canonical_sha256(output),
        "reference_scores": references,
        "observed_scores": observed,
        "absolute_errors": errors,
    }
    return CalibrationRecordV3(
        calibration_id=case.calibration_id,
        development_case_sha256=case.case_sha256,
        configuration_sha256=configuration.configuration_sha256,
        model_output_sha256=canonical_sha256(output),
        reference_scores=references,
        observed_scores=observed,
        absolute_errors=errors,
        record_sha256=canonical_sha256(payload),
    )


def freeze_calibration_thresholds_v3(
    configuration: ModelJudgeConfigurationV3,
    records: Sequence[CalibrationRecordV3],
    thresholds: CalibrationThresholdsV3,
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
    if len(records) < thresholds.minimum_development_cases:
        raise ValueError("insufficient development calibration records")
    if len({item.calibration_id for item in records}) != len(records):
        raise ValueError("development calibration IDs must be unique")
    if any(
        item.configuration_sha256 != configuration.configuration_sha256
        for item in records
    ):
        raise ValueError("calibration record uses a different judge configuration")
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
    payload = {
        "schema_version": "3.0",
        "protocol_sha256": configuration.bindings.protocol_sha256,
        "configuration_sha256": configuration.configuration_sha256,
        "thresholds_sha256": thresholds.thresholds_sha256,
        "development_record_sha256s": record_hashes,
        "maximum_observed_errors": maxima,
    }
    return CalibrationFreezeV3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        configuration_sha256=configuration.configuration_sha256,
        thresholds_sha256=thresholds.thresholds_sha256,
        development_record_sha256s=record_hashes,
        maximum_observed_errors=maxima,
        calibration_sha256=canonical_sha256(payload),
    )


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

    judgments: list[JudgmentRecordV3] = []
    for answer in packet.answers:
        output = _invoke_and_validate(
            configuration,
            answer,
            phase="held_out_scoring",
            calibration_sha256=calibration.calibration_sha256,
            judge=judge,
        )
        model_scores = {item.metric: item.score for item in output.verdicts}
        deterministic_scores = score_deterministic_metrics_v3(answer)
        scores = {**model_scores, **deterministic_scores}
        score_sources: dict[
            EvaluationMetricV3,
            Literal["deterministic", "model_judge", "human_review"],
        ] = {
            **{metric: "model_judge" for metric in model_scores},
            **{metric: "deterministic" for metric in deterministic_scores},
        }
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
            )
        )
    return tuple(judgments)


def score_deterministic_metrics_v3(
    answer: BlindedAnswerV3,
) -> dict[EvaluationMetricV3, float]:
    """Score citation metrics by exact membership in frozen rubric evidence."""

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


def validate_model_judge_output_v3(
    raw_output: object,
    answer: BlindedAnswerV3,
) -> ModelJudgeOutputV3:
    if isinstance(raw_output, (str, bytes, bytearray)):
        output = ModelJudgeOutputV3.model_validate_json(raw_output)
    elif isinstance(raw_output, ModelJudgeOutputV3):
        output = ModelJudgeOutputV3.model_validate(raw_output.model_dump(mode="json"))
    else:
        output = ModelJudgeOutputV3.model_validate(raw_output)
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
