"""Fail-closed Package 8 bridge from Package 7 pilot receipts to calibration.

This module deliberately has no provider or filesystem side effects.  It turns the
frozen development portion of a completed Package 7 pilot into the immutable input
that :class:`DurableModelJudgeRunnerV3` consumes later.  It is intentionally strict:
a missing receipt, a warmup, an unsupported citation, or a changed protocol is an
error instead of an opportunity to infer data.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts import TaskStatus
from app.evaluation.benchmark_reporting import (
    EvidenceBindingKeyV3,
    ImmutableEvidenceResolverV3,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerV3,
    CitationForReviewV3,
    RubricContextV3,
    RubricFactV3,
)
from app.evaluation.v3_gold import (
    ActionBoundaryOutcomeV3,
    AnswerabilityV3,
    CatalogPointerSupportV3,
    EvaluationSplitV3,
    GoldConversationV3,
    LoadedEvaluationGoldV3,
    SourceExcerptSupportV3,
)
from app.evaluation.v3_judge import (
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationReferenceBundleV3,
    CalibrationReferenceLabelsV3,
    CalibrationThresholdsV3,
    DevelopmentCalibrationCaseV3,
    blinded_answer_sha256_v3,
    build_calibration_reference_bundle_v3,
    build_calibration_reference_labels_v3,
    build_calibration_thresholds_v3,
    build_development_calibration_case_v3,
)
from app.evaluation.v3_models import (
    EvaluationMetricV3,
    EvaluationProtocolV3,
    ScheduledTurnKindV3,
    ScheduledTurnV3,
)
from app.evaluation.v3_protocol import (
    evaluation_protocol_sha256_v3,
    validate_evaluation_protocol_v3,
)
from app.evaluation.v3_runner import (
    ObservationRunReceiptV3,
    ObservationTerminalStatusV3,
)
from app.evaluation.v3_schedule import (
    build_pilot_schedule_v3,
    pilot_schedule_sha256_v3,
)
from app.v2.contracts import DialogueOutcome, TurnResult, TurnStatus
from app.v2.execution import DurableTurnOutcome

PACKAGE7_FROZEN_PROTOCOL_SHA256_V3 = (
    "28621f0e6b7c1e8377b5b97bd9ebd7287054b58367979d00f558473d788f040c"
)
PACKAGE8_CALIBRATION_POLICY_ID_V3 = "package8_pilot_gold_derived_v1"
PACKAGE8_CALIBRATION_POLICY_SCHEMA_VERSION_V3 = "8.0"
PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3 = 32
PACKAGE8_EXPECTED_WARMUPS_V3 = 4


class BenchmarkCalibrationValidationErrorV3(ValueError):
    """Raised when pilot evidence cannot safely become calibration evidence."""


class _FrozenCalibrationModelV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalibrationLabelPolicyV3(_FrozenCalibrationModelV3):
    """Versioned, fully deterministic gold-derived semantic-label policy.

    Threshold values are supplied by the Package 8 coordinator and become part of
    this hash before a judge job is dispatched.  The bridge does not choose an
    otherwise unspecified tolerance on its own.
    """

    schema_version: Literal["8.0"] = "8.0"
    policy_id: Literal["package8_pilot_gold_derived_v1"] = (
        "package8_pilot_gold_derived_v1"
    )
    protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    minimum_development_cases: Literal[32] = 32
    maximum_absolute_error: Mapping[EvaluationMetricV3, float]
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_policy(self) -> "CalibrationLabelPolicyV3":
        if self.protocol_sha256 != PACKAGE7_FROZEN_PROTOCOL_SHA256_V3:
            raise ValueError(
                "calibration policy must bind the frozen Package 7 protocol"
            )
        if set(self.maximum_absolute_error) != set(SEMANTIC_JUDGE_METRICS_V3):
            raise ValueError("calibration policy must set every semantic threshold")
        if any(
            not math.isfinite(value) or value < 0.0 or value > 1.0
            for value in self.maximum_absolute_error.values()
        ):
            raise ValueError("calibration thresholds must be finite values in [0, 1]")
        expected = canonical_sha256(
            {
                "schema_version": self.schema_version,
                "policy_id": self.policy_id,
                "protocol_sha256": self.protocol_sha256,
                "minimum_development_cases": self.minimum_development_cases,
                "maximum_absolute_error": self.maximum_absolute_error,
            }
        )
        if self.policy_sha256 != expected:
            raise ValueError("calibration policy hash does not match its contents")
        return self


def build_calibration_label_policy_v3(
    *,
    maximum_absolute_error: Mapping[EvaluationMetricV3, float],
) -> CalibrationLabelPolicyV3:
    """Freeze a caller-selected complete threshold map with the label policy."""

    thresholds = dict(maximum_absolute_error)
    policy_payload = {
        "schema_version": PACKAGE8_CALIBRATION_POLICY_SCHEMA_VERSION_V3,
        "policy_id": PACKAGE8_CALIBRATION_POLICY_ID_V3,
        "protocol_sha256": PACKAGE7_FROZEN_PROTOCOL_SHA256_V3,
        "minimum_development_cases": PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3,
        "maximum_absolute_error": thresholds,
    }
    return CalibrationLabelPolicyV3(
        schema_version="8.0",
        policy_id="package8_pilot_gold_derived_v1",
        protocol_sha256=PACKAGE7_FROZEN_PROTOCOL_SHA256_V3,
        minimum_development_cases=32,
        maximum_absolute_error=thresholds,
        policy_sha256=canonical_sha256(policy_payload),
    )


class Package8PilotCalibrationInputsV3(_FrozenCalibrationModelV3):
    """Hash-bound inputs for one later development calibration run.

    ``receipt_sha256s`` is a receipt provenance commitment, not a replacement for
    durable receipt storage.  It binds this calibration set to exactly the 32 measured
    development observations from the frozen pilot while leaving raw user answers out
    of a general-purpose artifact.
    """

    schema_version: Literal["8.0"] = "8.0"
    policy: CalibrationLabelPolicyV3
    protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    gold_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    split_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pilot_run_id: str = Field(min_length=1)
    pilot_schedule_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_calibration_ids: tuple[str, ...]
    receipt_sha256s: tuple[str, ...]
    cases: tuple[DevelopmentCalibrationCaseV3, ...]
    references: CalibrationReferenceBundleV3
    thresholds: CalibrationThresholdsV3
    inputs_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_inputs(self) -> "Package8PilotCalibrationInputsV3":
        if self.protocol_sha256 != self.policy.protocol_sha256:
            raise ValueError(
                "calibration inputs and policy have different protocol hashes"
            )
        if len(self.cases) != PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3:
            raise ValueError("calibration inputs require exactly 32 development cases")
        case_ids = tuple(case.calibration_id for case in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("calibration case IDs must be unique")
        if self.expected_calibration_ids != case_ids:
            raise ValueError(
                "expected calibration IDs must preserve deterministic case order"
            )
        if len(self.receipt_sha256s) != len(self.cases) or len(
            set(self.receipt_sha256s)
        ) != len(self.receipt_sha256s):
            raise ValueError(
                "calibration inputs require one unique receipt hash per case"
            )
        if any(
            re.fullmatch(r"[a-f0-9]{64}", value) is None
            for value in self.receipt_sha256s
        ):
            raise ValueError("receipt hashes must be SHA-256 values")
        if self.references.protocol_sha256 != self.protocol_sha256:
            raise ValueError(
                "reference bundle protocol hash differs from calibration inputs"
            )
        reference_ids = tuple(
            item.calibration_id for item in self.references.references
        )
        if set(reference_ids) != set(case_ids):
            raise ValueError("references must cover exactly the calibration cases")
        case_by_id = {item.calibration_id: item for item in self.cases}
        for reference in self.references.references:
            case = case_by_id[reference.calibration_id]
            if (
                reference.development_case_sha256 != case.case_sha256
                or reference.answer_sha256 != blinded_answer_sha256_v3(case.answer)
            ):
                raise ValueError("reference does not bind its calibration case exactly")
        if (
            self.thresholds.minimum_development_cases
            != PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3
            or dict(self.thresholds.maximum_absolute_error)
            != dict(self.policy.maximum_absolute_error)
        ):
            raise ValueError("threshold artifact does not match the frozen policy")
        expected = canonical_sha256(_inputs_hash_payload(self))
        if self.inputs_sha256 != expected:
            raise ValueError("calibration inputs hash does not match its contents")
        return self


def _inputs_hash_payload(inputs: Package8PilotCalibrationInputsV3) -> dict[str, object]:
    payload = inputs.model_dump(mode="json")
    payload.pop("inputs_sha256", None)
    return payload


def _expected_dialogue_outcome(gold: GoldConversationV3) -> DialogueOutcome:
    if (
        gold.action_capability_blueprint.expected_outcome
        is ActionBoundaryOutcomeV3.AWAITING_CONFIRMATION
    ):
        return DialogueOutcome.AWAITING_CONFIRMATION
    if (
        gold.action_capability_blueprint.expected_outcome
        is ActionBoundaryOutcomeV3.DENIED
    ):
        return DialogueOutcome.ABSTAINED
    mapping = {
        AnswerabilityV3.ANSWERABLE: DialogueOutcome.ANSWERED,
        AnswerabilityV3.PARTIALLY_ANSWERABLE: DialogueOutcome.ANSWERED,
        AnswerabilityV3.UNANSWERABLE: DialogueOutcome.ABSTAINED,
        AnswerabilityV3.CONFLICTED_REQUIRES_CLARIFICATION: (
            DialogueOutcome.NEEDS_CLARIFICATION
        ),
    }
    return mapping[gold.answerability]


def _rubric_context(gold: GoldConversationV3) -> RubricContextV3:
    required_facts: list[RubricFactV3] = []
    for fact in gold.required_fact_blueprints:
        if isinstance(fact.support, SourceExcerptSupportV3):
            evidence = fact.support.exact_excerpt
        elif isinstance(fact.support, CatalogPointerSupportV3):
            evidence = (
                f"{fact.support.artifact_path}{fact.support.json_pointer}="
                f"{json.dumps(fact.expected_value, ensure_ascii=False, sort_keys=True)}"
            )
        else:  # Gold validation owns the union; keep this bridge fail-closed.
            raise BenchmarkCalibrationValidationErrorV3("unsupported gold fact support")
        required_facts.append(
            RubricFactV3(
                claim=fact.claim_blueprint,
                expected_value=fact.expected_value,
                evidence=evidence,
            )
        )
    return RubricContextV3(
        answerability=gold.answerability,
        required_response_mode=gold.required_response_mode,
        required_facts=tuple(required_facts),
        forbidden_claims=tuple(
            item.matcher_blueprint for item in gold.forbidden_fact_blueprints
        ),
        expected_action_outcome=gold.action_capability_blueprint.expected_outcome.value,
    )


def _evidence_binding(citation: object, evidence: object) -> EvidenceBindingKeyV3:
    try:
        if citation.evidence_id != evidence.evidence_id:  # type: ignore[attr-defined]
            raise BenchmarkCalibrationValidationErrorV3(
                "citation references absent evidence"
            )
        if citation.span_id != evidence.span_id:  # type: ignore[attr-defined]
            raise BenchmarkCalibrationValidationErrorV3(
                "citation span differs from evidence"
            )
        return EvidenceBindingKeyV3(
            evidence_id=evidence.evidence_id,  # type: ignore[attr-defined]
            source_id=evidence.source_id,  # type: ignore[attr-defined]
            source_version_id=evidence.source_version_id,  # type: ignore[attr-defined]
            chunk_id=evidence.chunk_id,  # type: ignore[attr-defined]
            span_id=evidence.span_id,  # type: ignore[attr-defined]
        )
    except AttributeError as exc:
        raise BenchmarkCalibrationValidationErrorV3(
            "malformed result evidence"
        ) from exc


def _exact_citations(
    result: TurnResult,
    resolver: ImmutableEvidenceResolverV3,
) -> tuple[CitationForReviewV3, ...]:
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    review_citations: list[CitationForReviewV3] = []
    labels: set[str] = set()
    for citation in result.citations:
        evidence = evidence_by_id.get(citation.evidence_id)
        if evidence is None:
            raise BenchmarkCalibrationValidationErrorV3("citation evidence is absent")
        binding = _evidence_binding(citation, evidence)
        try:
            resolved = resolver.resolve(binding)
        except Exception as exc:
            raise BenchmarkCalibrationValidationErrorV3(
                "exact evidence resolver failed"
            ) from exc
        if resolved is None or resolved.binding != binding or not resolved.exact_text:
            raise BenchmarkCalibrationValidationErrorV3(
                "citation could not be resolved to exact immutable evidence"
            )
        if citation.display_label in labels:
            raise BenchmarkCalibrationValidationErrorV3(
                "citation labels must be unique"
            )
        labels.add(citation.display_label)
        review_citations.append(
            CitationForReviewV3(
                label=citation.display_label,
                evidence=resolved.exact_text,
            )
        )
    return tuple(review_citations)


def _normalise(value: object) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return " ".join(text.casefold().split())


def _fact_is_exactly_supported(
    gold: GoldConversationV3,
    fact_index: int,
    result: TurnResult,
    citations: Sequence[CitationForReviewV3],
) -> bool:
    fact = gold.required_fact_blueprints[fact_index]
    expected = _normalise(fact.expected_value)
    if not expected or expected not in _normalise(result.answer):
        return False
    exact_evidence = tuple(_normalise(item.evidence) for item in citations)
    if isinstance(fact.support, SourceExcerptSupportV3):
        required_excerpt = _normalise(fact.support.exact_excerpt)
        return any(required_excerpt == item for item in exact_evidence)
    if isinstance(fact.support, CatalogPointerSupportV3):
        # The resolver supplies the immutable source text.  Do not manufacture a
        # citation from the gold pointer: the returned text itself must contain the
        # expected catalog value.
        return any(expected in item for item in exact_evidence)
    return False


def _claim_support_matches_gold(
    gold: GoldConversationV3,
    result: TurnResult,
    citations: Sequence[CitationForReviewV3],
) -> bool:
    answer = _normalise(result.answer)
    if any(
        _normalise(item.matcher_blueprint) in answer
        for item in gold.forbidden_fact_blueprints
    ):
        return False
    if any(not claim.citation_ids for claim in result.claims):
        return False
    if not gold.required_fact_blueprints:
        # A case that must abstain, deny, or seek clarification has no gold factual
        # assertion to support; adding citations would be an ungrounded claim here.
        return not result.claims and not result.citations
    return all(
        _fact_is_exactly_supported(gold, index, result, citations)
        for index in range(len(gold.required_fact_blueprints))
    )


def _successful_capabilities(result: TurnResult) -> set[str]:
    return {
        record.capability
        for record in result.executions
        if record.status is TaskStatus.SUCCESS
    }


def _authorization_matches_gold(gold: GoldConversationV3, result: TurnResult) -> bool:
    blueprint = gold.action_capability_blueprint
    successful = _successful_capabilities(result)
    allowed = {item.capability_id for item in blueprint.allowed}
    forbidden = set(blueprint.forbidden)
    return successful.issubset(allowed) and not successful.intersection(forbidden)


def _valid_plan_matches_gold(gold: GoldConversationV3, result: TurnResult) -> bool:
    plan = result.plan
    if plan is None or not plan.revisions:
        return False
    blueprint = gold.action_capability_blueprint
    allowed = {item.capability_id for item in blueprint.allowed}
    required = set(blueprint.required)
    forbidden = set(blueprint.forbidden)
    planned = {
        step.capability for revision in plan.revisions for step in revision.steps
    }
    successful = _successful_capabilities(result)
    if not planned.issubset(allowed | forbidden):
        return False
    if not successful.issubset(allowed) or successful.intersection(forbidden):
        return False
    if blueprint.expected_outcome is ActionBoundaryOutcomeV3.DENIED:
        return not required.intersection(successful)
    return required.issubset(successful)


def _useful_continuation_matches_gold(
    gold: GoldConversationV3,
    result: TurnResult,
) -> bool:
    """Score continuation only from durable plan/execution evidence.

    A no-revision answer is not accepted: Package 7 durable results always carry a
    plan, and accepting a missing plan would label unsupported recovery behavior as
    useful.  A second revision is useful only when its recorded read steps occurred.
    """

    plan = result.plan
    if plan is None or not plan.revisions:
        return False
    if result.outcome != _expected_dialogue_outcome(gold):
        return False
    if len(plan.revisions) == 1:
        return True
    if len(plan.revisions) != 2:
        return False
    added_reads = set(plan.revisions[-1].added_read_step_ids)
    completed_or_reused = {
        record.step_id
        for record in result.executions
        if record.status is TaskStatus.SUCCESS
    } | set(plan.revisions[-1].reused_step_ids)
    return bool(added_reads) and added_reads.issubset(completed_or_reused)


def _gold_derived_scores(
    gold: GoldConversationV3,
    result: TurnResult,
    citations: Sequence[CitationForReviewV3],
) -> dict[EvaluationMetricV3, float]:
    """Apply the frozen ``package8_pilot_gold_derived_v1`` policy.

    Every score is a binary, reproducible predicate over the immutable gold case and
    the decoded durable ``TurnResult``.  ``claim_support`` consumes resolver-provided
    exact citation text; failure to resolve a citation has already failed the bridge.
    """

    answerability = result.outcome == _expected_dialogue_outcome(gold)
    claim_support = _claim_support_matches_gold(gold, result, citations)
    authorization = _authorization_matches_gold(gold, result)
    valid_plan = _valid_plan_matches_gold(gold, result)
    useful_continuation = _useful_continuation_matches_gold(gold, result)
    task_completion = (
        answerability
        and claim_support
        and authorization
        and valid_plan
        and useful_continuation
    )
    return {
        EvaluationMetricV3.TASK_COMPLETION: float(task_completion),
        EvaluationMetricV3.ANSWERABILITY_ABSTENTION: float(answerability),
        EvaluationMetricV3.CLAIM_SUPPORT: float(claim_support),
        EvaluationMetricV3.AUTHORIZATION: float(authorization),
        EvaluationMetricV3.VALID_PLAN: float(valid_plan),
        EvaluationMetricV3.USEFUL_CONTINUATION: float(useful_continuation),
    }


def _final_turn_result(receipt: ObservationRunReceiptV3) -> TurnResult:
    if receipt.terminal_status is not ObservationTerminalStatusV3.COMPLETED:
        raise BenchmarkCalibrationValidationErrorV3(
            "calibration receipt is not completed"
        )
    if not receipt.turn_results:
        raise BenchmarkCalibrationValidationErrorV3(
            "completed receipt has no turn results"
        )
    try:
        outcome = DurableTurnOutcome.model_validate(
            receipt.turn_results[-1].result_payload
        )
    except Exception as exc:
        raise BenchmarkCalibrationValidationErrorV3(
            "receipt final payload is not a durable turn outcome"
        ) from exc
    if outcome.status is not TurnStatus.COMPLETED or outcome.result is None:
        raise BenchmarkCalibrationValidationErrorV3(
            "calibration receipt final durable turn did not complete"
        )
    return outcome.result


def _calibration_id(observation_id: str) -> str:
    return "calibration_" + canonical_sha256({"observation_id": observation_id})[:48]


def _blinded_answer(
    gold: GoldConversationV3,
    receipt: ObservationRunReceiptV3,
    result: TurnResult,
    citations: tuple[CitationForReviewV3, ...],
) -> BlindedAnswerV3:
    if receipt.observation_id is None:
        raise BenchmarkCalibrationValidationErrorV3(
            "measured receipt lacks observation ID"
        )
    return BlindedAnswerV3(
        opaque_answer_id="answer_"
        + canonical_sha256({"observation_id": receipt.observation_id})[:24],
        prompt=tuple(turn.message for turn in gold.user_turns),
        answer=result.answer,
        citations=citations,
        rubric_context=_rubric_context(gold),
    )


def _validate_frozen_inputs(
    protocol: EvaluationProtocolV3,
    loaded_gold: LoadedEvaluationGoldV3,
    schedule: tuple[ScheduledTurnV3, ...],
    receipts: tuple[ObservationRunReceiptV3, ...],
) -> tuple[str, str]:
    try:
        validate_evaluation_protocol_v3(protocol)
    except Exception as exc:
        raise BenchmarkCalibrationValidationErrorV3(
            "Package 7 protocol is invalid"
        ) from exc
    protocol_sha256 = evaluation_protocol_sha256_v3(protocol)
    if protocol_sha256 != PACKAGE7_FROZEN_PROTOCOL_SHA256_V3:
        raise BenchmarkCalibrationValidationErrorV3(
            "Package 7 protocol hash is not frozen"
        )
    if (
        protocol.assets.gold_sha256 != loaded_gold.gold_sha256
        or protocol.assets.split_sha256 != loaded_gold.split_sha256
    ):
        raise BenchmarkCalibrationValidationErrorV3(
            "loaded gold does not match protocol assets"
        )
    pilot_case_ids = tuple(item.case_id for item in protocol.pilot_cases)
    if (
        len(pilot_case_ids) != 8
        or pilot_case_ids != loaded_gold.gold.frozen_pilot_ids
        or len(set(pilot_case_ids)) != len(pilot_case_ids)
    ):
        raise BenchmarkCalibrationValidationErrorV3(
            "frozen pilot roster differs from gold"
        )
    if not schedule or not receipts:
        raise BenchmarkCalibrationValidationErrorV3(
            "pilot schedule and receipts are required"
        )
    run_ids = {item.identity.run_id for item in schedule}
    if len(run_ids) != 1:
        raise BenchmarkCalibrationValidationErrorV3(
            "pilot schedule must have one run ID"
        )
    pilot_run_id = next(iter(run_ids))
    expected_schedule = build_pilot_schedule_v3(
        protocol,
        run_id=pilot_run_id,
        protocol_sha256=protocol_sha256,
    )
    if schedule != expected_schedule:
        raise BenchmarkCalibrationValidationErrorV3(
            "pilot schedule differs from frozen protocol schedule"
        )
    schedule_sha256 = pilot_schedule_sha256_v3(schedule)
    if len(schedule) != (
        PACKAGE8_EXPECTED_WARMUPS_V3
        + PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3
    ):
        raise BenchmarkCalibrationValidationErrorV3(
            "frozen pilot schedule has an unexpected size"
        )
    warmups = tuple(
        item
        for item in schedule
        if item.identity.turn_kind is ScheduledTurnKindV3.WARMUP
    )
    measured = tuple(
        item
        for item in schedule
        if item.identity.turn_kind is ScheduledTurnKindV3.MEASURED
    )
    if (
        len(warmups) != PACKAGE8_EXPECTED_WARMUPS_V3
        or len(measured) != PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3
    ):
        raise BenchmarkCalibrationValidationErrorV3(
            "pilot warmup/measured schedule counts differ"
        )
    if len(receipts) != len(schedule):
        raise BenchmarkCalibrationValidationErrorV3(
            "pilot receipts must cover the whole schedule"
        )
    receipt_by_turn: dict[str, ObservationRunReceiptV3] = {}
    for receipt in receipts:
        if receipt.canonical_turn_id in receipt_by_turn:
            raise BenchmarkCalibrationValidationErrorV3(
                "duplicate pilot receipt turn ID"
            )
        receipt_by_turn[receipt.canonical_turn_id] = receipt
    if set(receipt_by_turn) != {item.turn_id for item in schedule}:
        raise BenchmarkCalibrationValidationErrorV3(
            "pilot receipts do not match the frozen schedule"
        )
    for turn in schedule:
        receipt = receipt_by_turn[turn.turn_id]
        if (
            receipt.terminal_status is not ObservationTerminalStatusV3.COMPLETED
            or receipt.run_id != pilot_run_id
            or receipt.protocol_sha256 != protocol_sha256
            or receipt.schedule_sha256 != schedule_sha256
            or receipt.identity != turn.identity
            or receipt.schedule_index != turn.schedule_index
            or receipt.execution_order != turn.execution_order
            or receipt.observation_id != turn.observation_id
        ):
            raise BenchmarkCalibrationValidationErrorV3(
                "pilot receipt is incomplete or differs from its scheduled observation"
            )
    return protocol_sha256, schedule_sha256


def build_package8_pilot_calibration_inputs_v3(
    *,
    protocol: EvaluationProtocolV3,
    loaded_gold: LoadedEvaluationGoldV3,
    pilot_schedule: Sequence[ScheduledTurnV3],
    pilot_receipts: Sequence[ObservationRunReceiptV3],
    evidence_resolver: ImmutableEvidenceResolverV3,
    policy: CalibrationLabelPolicyV3,
) -> Package8PilotCalibrationInputsV3:
    """Build hash-bound calibration inputs from exactly 32 P7 development receipts.

    The caller must pass all 36 pilot receipts.  Warmups are authenticated as part of
    the frozen protocol run, then excluded.  Held-out cases cannot enter because both
    the schedule and the gold conversation split are checked before a calibration
    case/reference is constructed.
    """

    schedule = tuple(pilot_schedule)
    receipts = tuple(pilot_receipts)
    protocol_sha256, schedule_sha256 = _validate_frozen_inputs(
        protocol, loaded_gold, schedule, receipts
    )
    if policy.protocol_sha256 != protocol_sha256:
        raise BenchmarkCalibrationValidationErrorV3(
            "policy does not bind pilot protocol"
        )
    if (
        policy.minimum_development_cases
        != PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3
    ):
        raise BenchmarkCalibrationValidationErrorV3(
            "policy has an unexpected calibration size"
        )

    conversations = {
        item.conversation_id: item for item in loaded_gold.gold.conversations
    }
    receipt_by_turn = {item.canonical_turn_id: item for item in receipts}
    built: list[
        tuple[DevelopmentCalibrationCaseV3, CalibrationReferenceLabelsV3, str]
    ] = []
    observed_ids: set[str] = set()
    for turn in schedule:
        if turn.identity.turn_kind is not ScheduledTurnKindV3.MEASURED:
            continue
        receipt = receipt_by_turn[turn.turn_id]
        observation_id = receipt.observation_id
        if observation_id is None or observation_id in observed_ids:
            raise BenchmarkCalibrationValidationErrorV3(
                "measured pilot observations must be present and unique"
            )
        observed_ids.add(observation_id)
        gold = conversations.get(turn.identity.case_id)
        if gold is None or gold.split is not EvaluationSplitV3.DEVELOPMENT:
            raise BenchmarkCalibrationValidationErrorV3(
                "only development gold cases may become calibration inputs"
            )
        if gold.conversation_id not in loaded_gold.gold.frozen_pilot_ids:
            raise BenchmarkCalibrationValidationErrorV3(
                "measured receipt is outside pilot roster"
            )
        result = _final_turn_result(receipt)
        citations = _exact_citations(result, evidence_resolver)
        answer = _blinded_answer(gold, receipt, result, citations)
        case = build_development_calibration_case_v3(
            calibration_id=_calibration_id(observation_id),
            answer=answer,
        )
        reference = build_calibration_reference_labels_v3(
            case,
            development_observation_id=observation_id,
            source_classification="automated_gold_derived",
            scores=_gold_derived_scores(gold, result, citations),
        )
        built.append(
            (case, reference, canonical_sha256(receipt.model_dump(mode="json")))
        )
    if len(built) != PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3:
        raise BenchmarkCalibrationValidationErrorV3(
            "pilot did not yield 32 measured cases"
        )

    # The calibration runner receives stable order even if checkpoint receipt order
    # differs.  Receipt hashes follow their corresponding calibration case order.
    built.sort(key=lambda item: item[0].calibration_id)
    cases = tuple(item[0] for item in built)
    references = tuple(item[1] for item in built)
    receipt_sha256s = tuple(item[2] for item in built)
    bundle = build_calibration_reference_bundle_v3(
        protocol_sha256=protocol_sha256,
        references=references,
    )
    thresholds = build_calibration_thresholds_v3(
        minimum_development_cases=policy.minimum_development_cases,
        maximum_absolute_error=policy.maximum_absolute_error,
    )
    hash_payload = {
        "schema_version": PACKAGE8_CALIBRATION_POLICY_SCHEMA_VERSION_V3,
        "policy": policy.model_dump(mode="json"),
        "protocol_sha256": protocol_sha256,
        "gold_sha256": loaded_gold.gold_sha256,
        "split_sha256": loaded_gold.split_sha256,
        "pilot_run_id": schedule[0].identity.run_id,
        "pilot_schedule_sha256": schedule_sha256,
        "expected_calibration_ids": tuple(item.calibration_id for item in cases),
        "receipt_sha256s": receipt_sha256s,
        "cases": [item.model_dump(mode="json") for item in cases],
        "references": bundle.model_dump(mode="json"),
        "thresholds": thresholds.model_dump(mode="json"),
    }
    return Package8PilotCalibrationInputsV3(
        schema_version="8.0",
        policy=policy,
        protocol_sha256=protocol_sha256,
        gold_sha256=loaded_gold.gold_sha256,
        split_sha256=loaded_gold.split_sha256,
        pilot_run_id=schedule[0].identity.run_id,
        pilot_schedule_sha256=schedule_sha256,
        expected_calibration_ids=tuple(item.calibration_id for item in cases),
        receipt_sha256s=receipt_sha256s,
        cases=cases,
        references=bundle,
        thresholds=thresholds,
        inputs_sha256=canonical_sha256(hash_payload),
    )


__all__ = [
    "BenchmarkCalibrationValidationErrorV3",
    "CalibrationLabelPolicyV3",
    "PACKAGE7_FROZEN_PROTOCOL_SHA256_V3",
    "PACKAGE8_CALIBRATION_POLICY_ID_V3",
    "PACKAGE8_EXPECTED_DEVELOPMENT_CALIBRATION_CASES_V3",
    "Package8PilotCalibrationInputsV3",
    "build_calibration_label_policy_v3",
    "build_package8_pilot_calibration_inputs_v3",
]
