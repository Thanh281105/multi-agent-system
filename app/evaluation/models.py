"""Typed benchmark inputs, observations, scores, and reports."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts import TaskStatus


class EvaluationCategory(StrEnum):
    SIMPLE = "simple"
    COMPLEX = "complex"
    MULTI_DOMAIN = "multi_domain"
    MISSING_DATA = "missing_data"
    TOOL_FAILURE = "tool_failure"
    AMBIGUOUS = "ambiguous"
    IRRELEVANT = "irrelevant"


class AssertionKind(StrEnum):
    CONTAINS = "contains"
    EXCLUDES = "excludes"
    ERROR_CODE_PRESENT = "error_code_present"
    SELECTED_PRODUCT_ID = "selected_product_id"
    PROVENANCE_AT_LEAST = "provenance_at_least"


class ExpectedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    count: int = Field(default=1, ge=1, le=10)


class AnswerAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AssertionKind
    value: str | int
    critical: bool = True

    @model_validator(mode="after")
    def validate_value_type(self) -> AnswerAssertion:
        if self.kind in {
            AssertionKind.CONTAINS,
            AssertionKind.EXCLUDES,
            AssertionKind.ERROR_CODE_PRESENT,
        }:
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError("text assertions require a non-empty string")
        elif not isinstance(self.value, int) or isinstance(self.value, bool):
            raise ValueError("numeric assertions require an integer")
        return self


class FailureInjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    action: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    error_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    recoverable: bool


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    category: EvaluationCategory
    message: str = Field(min_length=2, max_length=2_000)
    accepted_intents: tuple[str, ...] = Field(min_length=1)
    expected_actions: tuple[ExpectedAction, ...] = ()
    accepted_statuses: tuple[TaskStatus, ...] = Field(min_length=1)
    relevant_product_ids: tuple[int, ...] | None = None
    answer_assertions: tuple[AnswerAssertion, ...] = Field(min_length=1)
    failure_injection: FailureInjection | None = None

    @model_validator(mode="after")
    def validate_gold_labels(self) -> EvalCase:
        if len(self.accepted_intents) != len(set(self.accepted_intents)):
            raise ValueError("accepted intents must be unique")
        action_names = [item.action for item in self.expected_actions]
        if len(action_names) != len(set(action_names)):
            raise ValueError("expected actions must use one entry per action")
        if len(self.accepted_statuses) != len(set(self.accepted_statuses)):
            raise ValueError("accepted statuses must be unique")
        if any(
            status in {TaskStatus.PENDING, TaskStatus.RUNNING}
            for status in self.accepted_statuses
        ):
            raise ValueError("gold statuses must be terminal")
        if self.relevant_product_ids is not None:
            if any(product_id < 1 for product_id in self.relevant_product_ids):
                raise ValueError("relevant product IDs must be positive")
            if len(self.relevant_product_ids) != len(set(self.relevant_product_ids)):
                raise ValueError("relevant product IDs must be unique")
        return self


class EvaluationCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    dataset_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    sample_data: Literal[True] = True
    curation_note: str = Field(min_length=1, max_length=1_000)
    cases: tuple[EvalCase, ...] = Field(min_length=1, max_length=500)


class BaselineManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    baseline_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    status: Literal[
        "baseline_unavailable",
        "scripted_regression",
        "real_model_captured",
    ]
    reason: str = Field(min_length=1, max_length=2_000)
    captured_case_count: int = Field(default=0, ge=0)
    model: str | None = None
    prompt_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    tool_schema_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    dataset_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    artifact_path: str | None = Field(default=None, min_length=1)
    artifact_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    captured_at: datetime | None = None
    git_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")

    @model_validator(mode="after")
    def validate_evidence_state(self) -> BaselineManifest:
        if self.status == "baseline_unavailable" and (
            self.captured_case_count != 0
            or self.model is not None
            or self.prompt_sha256 is not None
            or self.tool_schema_sha256 is not None
            or self.dataset_sha256 is not None
            or self.artifact_path is not None
            or self.artifact_sha256 is not None
            or self.captured_at is not None
            or self.git_revision is not None
        ):
            raise ValueError(
                "an unavailable baseline cannot claim captured model evidence"
            )
        if self.status == "real_model_captured":
            if self.captured_case_count < 1:
                raise ValueError("a captured baseline must include cases")
            if not self.model:
                raise ValueError("a captured baseline must include the model")
            if not self.prompt_sha256 or not self.tool_schema_sha256:
                raise ValueError("a captured baseline must hash prompt and tools")
            if not self.dataset_sha256:
                raise ValueError("a captured baseline must hash its dataset")
            if not self.artifact_path or not self.artifact_sha256:
                raise ValueError("a captured baseline must bind its artifact")
            if self.captured_at is None or self.git_revision is None:
                raise ValueError("a captured baseline must include capture metadata")
        return self


class EvaluationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system_id: Literal["multi_agent_offline"] = "multi_agent_offline"
    case_id: str
    category: EvaluationCategory
    repetition: int = Field(ge=0)
    status: TaskStatus
    predicted_intent: str
    actions: tuple[str, ...] = ()
    retrieved_product_ids: tuple[int, ...] = ()
    selected_product_id: int | None = None
    answer: str
    error_codes: tuple[str, ...] = ()
    provenance_count: int = Field(ge=0)
    agent_attempts: int = Field(ge=0)
    agent_failures: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    token_usage: int | None = Field(default=None, ge=0)
    llm_cost_usd: float | None = Field(default=None, ge=0)


class CaseScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: EvaluationCategory
    routing_correct: bool
    action_true_positives: int = Field(ge=0)
    predicted_action_count: int = Field(ge=0)
    expected_action_count: int = Field(ge=0)
    action_precision: float | None = Field(default=None, ge=0, le=1)
    action_recall: float | None = Field(default=None, ge=0, le=1)
    action_f1: float | None = Field(default=None, ge=0, le=1)
    exact_plan: bool
    task_success: bool
    failure_injected: bool
    recoverable_failure: bool
    assertions_passed: int = Field(ge=0)
    assertion_count: int = Field(ge=0)
    answer_assertion_accuracy: float | None = Field(default=None, ge=0, le=1)
    retrieval_evaluated: bool
    retrieval_true_positives: int = Field(ge=0)
    retrieved_count: int = Field(ge=0)
    relevant_count: int = Field(ge=0)
    retrieval_precision: float | None = Field(default=None, ge=0, le=1)
    retrieval_recall: float | None = Field(default=None, ge=0, le=1)
    retrieval_f1: float | None = Field(default=None, ge=0, le=1)
    empty_retrieval_correct: bool | None = None


class MetricSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | None
    sample_size: int = Field(ge=0)
    numerator: float | None = None
    denominator: float | None = Field(default=None, ge=0)
    unit: str
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def explain_unavailable_metric(self) -> MetricSummary:
        if self.value is None and not self.unavailable_reason:
            raise ValueError("unavailable metrics require an explicit reason")
        if self.value is not None and self.unavailable_reason is not None:
            raise ValueError("available metrics cannot have an unavailable reason")
        return self


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"] = "1.1"
    generated_at: datetime
    system_id: Literal["multi_agent_offline"] = "multi_agent_offline"
    application_version: str
    sut_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sut_source_files: tuple[str, ...] = Field(min_length=1)
    dataset_id: str
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sample_seed_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sample_data: Literal[True] = True
    sample_counts: dict[str, int]
    random_seed: Literal[42] = 42
    python_version: str
    runtime_platform: str
    repeats: int = Field(ge=1, le=20)
    baseline: BaselineManifest
    comparison_status: Literal[
        "baseline_unavailable",
        "scripted_regression_only",
        "real_model_captured",
    ]
    metrics: dict[str, MetricSummary]
    metrics_by_category: dict[str, dict[str, MetricSummary]]
    case_scores: tuple[CaseScore, ...]
    observations: tuple[EvaluationObservation, ...]
    limitations: tuple[str, ...] = Field(min_length=1)
