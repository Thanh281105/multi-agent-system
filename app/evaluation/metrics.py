"""Pure metric functions with explicit zero-denominator semantics."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence

from app.contracts import TaskStatus
from app.evaluation.models import (
    AnswerAssertion,
    AssertionKind,
    CaseScore,
    EvalCase,
    EvaluationObservation,
    MetricSummary,
)


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def assertion_passes(
    assertion: AnswerAssertion,
    observation: EvaluationObservation,
) -> bool:
    if assertion.kind == AssertionKind.CONTAINS:
        return _normalize_text(str(assertion.value)) in _normalize_text(
            observation.answer
        )
    if assertion.kind == AssertionKind.EXCLUDES:
        return _normalize_text(str(assertion.value)) not in _normalize_text(
            observation.answer
        )
    if assertion.kind == AssertionKind.ERROR_CODE_PRESENT:
        return str(assertion.value) in observation.error_codes
    if assertion.kind == AssertionKind.SELECTED_PRODUCT_ID:
        return observation.selected_product_id == int(assertion.value)
    if assertion.kind == AssertionKind.PROVENANCE_AT_LEAST:
        return observation.provenance_count >= int(assertion.value)
    raise AssertionError(f"unhandled assertion kind: {assertion.kind}")


def score_case(case: EvalCase, observation: EvaluationObservation) -> CaseScore:
    expected_actions = Counter(
        {item.action: item.count for item in case.expected_actions}
    )
    predicted_actions = Counter(observation.actions)
    action_true_positives = sum(
        min(count, expected_actions[action])
        for action, count in predicted_actions.items()
    )
    predicted_action_count = sum(predicted_actions.values())
    expected_action_count = sum(expected_actions.values())
    action_precision = _ratio(action_true_positives, predicted_action_count)
    action_recall = _ratio(action_true_positives, expected_action_count)

    assertion_results = [
        assertion_passes(assertion, observation) for assertion in case.answer_assertions
    ]
    critical_assertions_pass = all(
        passed
        for assertion, passed in zip(
            case.answer_assertions,
            assertion_results,
            strict=True,
        )
        if assertion.critical
    )
    assertions_passed = sum(assertion_results)

    retrieval_evaluated = case.relevant_product_ids is not None
    relevant_ids = set(case.relevant_product_ids or ())
    retrieved_ids = set(observation.retrieved_product_ids)
    retrieval_true_positives = len(relevant_ids & retrieved_ids)
    retrieval_precision = (
        _ratio(retrieval_true_positives, len(retrieved_ids))
        if retrieval_evaluated
        else None
    )
    retrieval_recall = (
        _ratio(retrieval_true_positives, len(relevant_ids))
        if retrieval_evaluated
        else None
    )
    empty_retrieval_correct = None
    if retrieval_evaluated and not relevant_ids:
        empty_retrieval_correct = not retrieved_ids

    return CaseScore(
        case_id=case.case_id,
        category=case.category,
        routing_correct=observation.predicted_intent in case.accepted_intents,
        action_true_positives=action_true_positives,
        predicted_action_count=predicted_action_count,
        expected_action_count=expected_action_count,
        action_precision=action_precision,
        action_recall=action_recall,
        action_f1=_f1(action_precision, action_recall),
        exact_plan=predicted_actions == expected_actions,
        task_success=(
            observation.status in case.accepted_statuses and critical_assertions_pass
        ),
        failure_injected=case.failure_injection is not None,
        recoverable_failure=(
            case.failure_injection is not None and case.failure_injection.recoverable
        ),
        assertions_passed=assertions_passed,
        assertion_count=len(assertion_results),
        answer_assertion_accuracy=_ratio(
            assertions_passed,
            len(assertion_results),
        ),
        retrieval_evaluated=retrieval_evaluated,
        retrieval_true_positives=retrieval_true_positives,
        retrieved_count=len(retrieved_ids) if retrieval_evaluated else 0,
        relevant_count=len(relevant_ids) if retrieval_evaluated else 0,
        retrieval_precision=retrieval_precision,
        retrieval_recall=retrieval_recall,
        retrieval_f1=_f1(retrieval_precision, retrieval_recall),
        empty_retrieval_correct=empty_retrieval_correct,
    )


def aggregate_metrics(
    scores: Sequence[CaseScore],
    observations: Sequence[EvaluationObservation],
) -> dict[str, MetricSummary]:
    canonical = {item.case_id: item for item in observations if item.repetition == 0}
    routing_correct = sum(item.routing_correct for item in scores)
    exact_plans = sum(item.exact_plan for item in scores)
    successful_tasks = sum(item.task_success for item in scores)
    action_true_positives = sum(item.action_true_positives for item in scores)
    predicted_actions = sum(item.predicted_action_count for item in scores)
    expected_actions = sum(item.expected_action_count for item in scores)
    action_precision = _ratio(action_true_positives, predicted_actions)
    action_recall = _ratio(action_true_positives, expected_actions)

    no_tool_scores = [item for item in scores if item.expected_action_count == 0]
    empty_retrieval_scores = [
        item for item in scores if item.retrieval_evaluated and item.relevant_count == 0
    ]
    retrieval_scores = [item for item in scores if item.retrieval_evaluated]
    retrieval_true_positives = sum(
        item.retrieval_true_positives for item in retrieval_scores
    )
    retrieved_count = sum(item.retrieved_count for item in retrieval_scores)
    relevant_count = sum(item.relevant_count for item in retrieval_scores)
    retrieval_precision = _ratio(retrieval_true_positives, retrieved_count)
    retrieval_recall = _ratio(retrieval_true_positives, relevant_count)

    assertion_passes = sum(item.assertions_passed for item in scores)
    assertion_count = sum(item.assertion_count for item in scores)
    attempts = sum(canonical[item.case_id].agent_attempts for item in scores)
    failures = sum(canonical[item.case_id].agent_failures for item in scores)
    provenance_cases = [
        canonical[item.case_id]
        for item in scores
        if item.expected_action_count > 0
        and canonical[item.case_id].status != TaskStatus.FAILED
    ]
    recoverable_failure_scores = [item for item in scores if item.recoverable_failure]
    latency_values = [item.latency_ms for item in observations]

    return {
        "routing_accuracy": _available_ratio(
            routing_correct,
            len(scores),
            unit="ratio",
        ),
        "tool_selection_precision": _optional_ratio(
            action_true_positives,
            predicted_actions,
            sample_size=len(scores),
            unit="ratio",
            reason="no predicted actions in this slice",
        ),
        "tool_selection_recall": _optional_ratio(
            action_true_positives,
            expected_actions,
            sample_size=len(scores),
            unit="ratio",
            reason="no expected actions in this slice",
        ),
        "tool_selection_f1": _derived_metric(
            _f1(action_precision, action_recall),
            sample_size=len(scores),
            unit="ratio",
            reason="precision or recall is undefined",
        ),
        "exact_plan_rate": _available_ratio(
            exact_plans,
            len(scores),
            unit="ratio",
        ),
        "no_tool_correctness": _optional_ratio(
            sum(item.exact_plan for item in no_tool_scores),
            len(no_tool_scores),
            sample_size=len(no_tool_scores),
            unit="ratio",
            reason="no no-tool cases in this slice",
        ),
        "task_success_rate": _available_ratio(
            successful_tasks,
            len(scores),
            unit="ratio",
        ),
        "answer_assertion_accuracy": _available_ratio(
            assertion_passes,
            assertion_count,
            unit="ratio",
        ),
        "retrieval_precision": _optional_ratio(
            retrieval_true_positives,
            retrieved_count,
            sample_size=len(retrieval_scores),
            unit="ratio",
            reason="no retrieved products in evaluated cases",
        ),
        "retrieval_recall": _optional_ratio(
            retrieval_true_positives,
            relevant_count,
            sample_size=len(retrieval_scores),
            unit="ratio",
            reason="no gold-relevant products in evaluated cases",
        ),
        "retrieval_f1": _derived_metric(
            _f1(retrieval_precision, retrieval_recall),
            sample_size=len(retrieval_scores),
            unit="ratio",
            reason="retrieval precision or recall is undefined",
        ),
        "empty_retrieval_correctness": _optional_ratio(
            sum(
                item.empty_retrieval_correct is True for item in empty_retrieval_scores
            ),
            len(empty_retrieval_scores),
            sample_size=len(empty_retrieval_scores),
            unit="ratio",
            reason="no explicitly empty retrieval cases in this slice",
        ),
        "agent_failure_rate": _optional_ratio(
            failures,
            attempts,
            sample_size=len(scores),
            unit="ratio",
            reason="no agent steps attempted in this slice",
        ),
        "partial_recovery_rate": _optional_ratio(
            sum(item.task_success for item in recoverable_failure_scores),
            len(recoverable_failure_scores),
            sample_size=len(recoverable_failure_scores),
            unit="ratio",
            reason="no recoverable injected-failure cases in this slice",
        ),
        "provenance_case_coverage": _optional_ratio(
            sum(item.provenance_count > 0 for item in provenance_cases),
            len(provenance_cases),
            sample_size=len(provenance_cases),
            unit="ratio",
            reason="no agent-backed cases in this slice",
        ),
        "offline_latency_p50_ms": _latency_metric(latency_values, 0.50),
        "offline_latency_p95_ms": _latency_metric(latency_values, 0.95),
        "token_usage": _unavailable(
            sample_size=len(observations),
            unit="tokens",
            reason=(
                "offline deterministic runtime made zero observed model calls; "
                "production token usage was not measured"
            ),
        ),
        "llm_cost_usd": _unavailable(
            sample_size=len(observations),
            unit="USD",
            reason=(
                "offline deterministic runtime made zero observed model calls; "
                "production provider cost was not measured"
            ),
        ),
    }


def _available_ratio(numerator: int, denominator: int, *, unit: str) -> MetricSummary:
    value = _ratio(numerator, denominator)
    if value is None:
        return _unavailable(
            sample_size=0,
            unit=unit,
            reason="metric slice is empty",
        )
    return MetricSummary(
        value=value,
        sample_size=denominator,
        numerator=float(numerator),
        denominator=float(denominator),
        unit=unit,
    )


def _optional_ratio(
    numerator: int,
    denominator: int,
    *,
    sample_size: int,
    unit: str,
    reason: str,
) -> MetricSummary:
    value = _ratio(numerator, denominator)
    if value is None:
        return _unavailable(sample_size=sample_size, unit=unit, reason=reason)
    return MetricSummary(
        value=value,
        sample_size=sample_size,
        numerator=float(numerator),
        denominator=float(denominator),
        unit=unit,
    )


def _derived_metric(
    value: float | None,
    *,
    sample_size: int,
    unit: str,
    reason: str,
) -> MetricSummary:
    if value is None:
        return _unavailable(sample_size=sample_size, unit=unit, reason=reason)
    return MetricSummary(value=value, sample_size=sample_size, unit=unit)


def _latency_metric(values: Sequence[float], quantile: float) -> MetricSummary:
    if not values:
        return _unavailable(
            sample_size=0,
            unit="ms",
            reason="no measured observations",
        )
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return MetricSummary(
        value=ordered[rank - 1],
        sample_size=len(ordered),
        unit="ms",
    )


def _unavailable(*, sample_size: int, unit: str, reason: str) -> MetricSummary:
    return MetricSummary(
        value=None,
        sample_size=sample_size,
        unit=unit,
        unavailable_reason=reason,
    )


def observations_for_categories(
    observations: Iterable[EvaluationObservation],
    case_ids: set[str],
) -> list[EvaluationObservation]:
    return [item for item in observations if item.case_id in case_ids]
