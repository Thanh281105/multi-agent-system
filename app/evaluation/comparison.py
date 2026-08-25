"""Paired, clustered-bootstrap, and robustness comparisons for evaluation v2."""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence

from app.evaluation.v2_models import (
    ComparisonMetric,
    ConfidenceIntervalV2,
    EvaluationObservationV2,
    EvaluationPhase,
    MetricDirection,
    PairedComparisonV2,
    RobustnessPolicy,
    RobustnessSummaryV2,
    RobustnessTransformSummaryV2,
    WinTieLossV2,
)

_BINARY_METRICS = frozenset(
    {
        ComparisonMetric.TASK_SUCCESS,
        ComparisonMetric.ROUTING_CORRECT,
        ComparisonMetric.EXACT_PLAN,
    }
)
_LOWER_IS_BETTER = frozenset(
    {
        ComparisonMetric.END_TO_END_LATENCY_MS,
        ComparisonMetric.TOTAL_TOKENS,
        ComparisonMetric.ESTIMATED_COST_USD,
    }
)
_TIE_TOLERANCE = 1e-12
_RobustnessRow = tuple[str, str, str, float, float, float]


def compare_variants(
    observations: Sequence[EvaluationObservationV2],
    *,
    baseline_variant_id: str,
    candidate_variant_id: str,
    metric: ComparisonMetric,
    phase: EvaluationPhase,
    bootstrap_samples: int = 5_000,
    random_seed: int = 42,
) -> PairedComparisonV2:
    if baseline_variant_id == candidate_variant_id:
        raise ValueError("paired comparison requires distinct variants")
    baseline = _variant_index(observations, baseline_variant_id, phase)
    candidate = _variant_index(observations, candidate_variant_id, phase)
    if not baseline or not candidate:
        raise ValueError("both variants require observations")
    if set(baseline) != set(candidate):
        missing_candidate = len(set(baseline) - set(candidate))
        missing_baseline = len(set(candidate) - set(baseline))
        raise ValueError(
            "paired variants have different case/repetition keys: "
            f"candidate_missing={missing_candidate}, "
            f"baseline_missing={missing_baseline}"
        )

    protocol_hashes = {
        observation.protocol_sha256
        for observation in (*baseline.values(), *candidate.values())
    }
    if len(protocol_hashes) != 1:
        raise ValueError("paired variants must use one identical protocol hash")

    included: list[tuple[str, float, float]] = []
    excluded_pair_count = 0
    for key in sorted(baseline):
        baseline_value = _metric_value(baseline[key], metric)
        candidate_value = _metric_value(candidate[key], metric)
        if baseline_value is None or candidate_value is None:
            excluded_pair_count += 1
            continue
        included.append((key[0], baseline_value, candidate_value))
    if not included:
        raise ValueError(f"metric {metric.value!r} has no comparable paired values")

    case_baseline = _case_means(included, value=lambda row: row[1])
    case_candidate = _case_means(included, value=lambda row: row[2])
    if set(case_baseline) != set(case_candidate):
        raise AssertionError("paired case aggregation diverged")
    case_ids = sorted(case_baseline)
    deltas = [case_candidate[case_id] - case_baseline[case_id] for case_id in case_ids]
    direction = (
        MetricDirection.LOWER_IS_BETTER
        if metric in _LOWER_IS_BETTER
        else MetricDirection.HIGHER_IS_BETTER
    )
    wins, ties, losses = _win_tie_loss(deltas, direction)
    confidence_interval = _clustered_bootstrap_interval(
        deltas,
        bootstrap_samples=bootstrap_samples,
        random_seed=random_seed,
    )

    return PairedComparisonV2(
        protocol_sha256=next(iter(protocol_hashes)),
        baseline_variant_id=baseline_variant_id,
        candidate_variant_id=candidate_variant_id,
        metric=metric,
        phase=phase,
        direction=direction,
        observation_pair_count=len(included),
        case_count=len(case_ids),
        excluded_pair_count=excluded_pair_count,
        baseline_mean=statistics.fmean(case_baseline.values()),
        candidate_mean=statistics.fmean(case_candidate.values()),
        mean_delta=statistics.fmean(deltas),
        median_delta=statistics.median(deltas),
        confidence_interval=confidence_interval,
        win_tie_loss=WinTieLossV2(wins=wins, ties=ties, losses=losses),
        paired_effect_size=_paired_effect_size(deltas),
        exact_two_sided_p_value=(
            _exact_two_sided_sign_test(wins, losses)
            if metric in _BINARY_METRICS
            else None
        ),
        notes=(
            "Delta is candidate minus baseline; direction controls win/loss.",
            "Bootstrap resamples independent base cases, not repetitions.",
        ),
    )


def summarize_robustness(
    observations: Sequence[EvaluationObservationV2],
    *,
    variant_id: str,
) -> RobustnessSummaryV2:
    selected = [
        item
        for item in observations
        if item.variant_id == variant_id and item.phase == EvaluationPhase.CORRECTNESS
    ]
    if not selected:
        raise ValueError(f"variant {variant_id!r} has no observations")
    selected_keys = [(item.case_id, item.repetition) for item in selected]
    if len(selected_keys) != len(set(selected_keys)):
        raise ValueError(f"variant {variant_id!r} has duplicate observation keys")
    protocol_hashes = {item.protocol_sha256 for item in selected}
    if len(protocol_hashes) != 1:
        raise ValueError("robustness observations must share one protocol hash")

    clean = {
        (item.case_id, item.repetition): item
        for item in selected
        if item.robustness_policy == RobustnessPolicy.CLEAN
    }
    transformed = [
        item for item in selected if item.robustness_policy != RobustnessPolicy.CLEAN
    ]
    if not clean or not transformed:
        raise ValueError("robustness summary requires clean and transformed cases")

    paired: list[_RobustnessRow] = []
    for item in transformed:
        if item.parent_case_id is None or item.transform_id is None:
            raise AssertionError("validated robustness observation lacks parent")
        parent = clean.get((item.parent_case_id, item.repetition))
        if parent is None:
            raise ValueError(
                "missing clean parent observation for "
                f"{item.case_id!r} repetition {item.repetition}"
            )
        paired.append(
            (
                item.transform_id,
                item.parent_case_id,
                item.case_id,
                float(item.task_success),
                float(item.predicted_intent == parent.predicted_intent),
                float(parent.task_success) - float(item.task_success),
            )
        )

    transform_summaries: list[RobustnessTransformSummaryV2] = []
    transform_degradation: dict[str, float] = {}
    for transform_id in sorted({item[0] for item in paired}):
        rows = [item for item in paired if item[0] == transform_id]
        case_success = _group_mean(
            rows,
            key=lambda row: row[2],
            value=lambda row: row[3],
        )
        case_consistency = _group_mean(
            rows,
            key=lambda row: row[2],
            value=lambda row: row[4],
        )
        case_degradation = _group_mean(
            rows,
            key=lambda row: row[2],
            value=lambda row: row[5],
        )
        transform_degradation[transform_id] = statistics.fmean(
            case_degradation.values()
        )
        transform_summaries.append(
            RobustnessTransformSummaryV2(
                transform_id=transform_id,
                case_count=len(case_success),
                task_success_rate=statistics.fmean(case_success.values()),
                intent_consistency_rate=statistics.fmean(case_consistency.values()),
                mean_task_success_degradation=transform_degradation[transform_id],
            )
        )

    overall_success = _group_mean(
        paired,
        key=lambda item: (item[0], item[2]),
        value=lambda item: item[3],
    )
    overall_consistency = _group_mean(
        paired,
        key=lambda item: (item[0], item[2]),
        value=lambda item: item[4],
    )
    overall_degradation = _group_mean(
        paired,
        key=lambda item: (item[0], item[2]),
        value=lambda item: item[5],
    )
    worst_transform_id = max(
        sorted(transform_degradation),
        key=transform_degradation.__getitem__,
    )
    clean_parent_ids = {item.parent_case_id for item in transformed}
    return RobustnessSummaryV2(
        protocol_sha256=next(iter(protocol_hashes)),
        variant_id=variant_id,
        clean_case_count=len(clean_parent_ids),
        transformed_case_count=len(overall_success),
        task_success_rate=statistics.fmean(overall_success.values()),
        intent_consistency_rate=statistics.fmean(overall_consistency.values()),
        mean_task_success_degradation=statistics.fmean(overall_degradation.values()),
        maximum_task_success_degradation=transform_degradation[worst_transform_id],
        worst_transform_id=worst_transform_id,
        transforms=tuple(transform_summaries),
    )


def _variant_index(
    observations: Sequence[EvaluationObservationV2],
    variant_id: str,
    phase: EvaluationPhase,
) -> dict[tuple[str, int], EvaluationObservationV2]:
    selected = [
        item
        for item in observations
        if item.variant_id == variant_id and item.phase == phase
    ]
    index = {(item.case_id, item.repetition): item for item in selected}
    if len(index) != len(selected):
        raise ValueError(f"variant {variant_id!r} has duplicate observation keys")
    return index


def _metric_value(
    observation: EvaluationObservationV2,
    metric: ComparisonMetric,
) -> float | None:
    if metric == ComparisonMetric.TASK_SUCCESS:
        return float(observation.task_success)
    if metric == ComparisonMetric.ROUTING_CORRECT:
        return float(observation.routing_correct)
    if metric == ComparisonMetric.EXACT_PLAN:
        return float(observation.exact_plan)
    if metric == ComparisonMetric.ANSWER_ASSERTION_ACCURACY:
        return observation.answer_assertion_accuracy
    if metric == ComparisonMetric.RETRIEVAL_F1:
        return observation.retrieval_f1
    if metric == ComparisonMetric.END_TO_END_LATENCY_MS:
        return observation.end_to_end_latency_ms
    if metric == ComparisonMetric.TOTAL_TOKENS:
        return float(observation.total_tokens)
    if metric == ComparisonMetric.ESTIMATED_COST_USD:
        return (
            float(observation.estimated_cost_usd)
            if observation.estimated_cost_usd is not None
            else None
        )
    raise AssertionError(f"unsupported comparison metric: {metric}")


def _case_means(
    rows: Sequence[tuple[str, float, float]],
    *,
    value: Callable[[tuple[str, float, float]], float],
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(value(row))
    return {case_id: statistics.fmean(values) for case_id, values in grouped.items()}


def _group_mean(
    rows: Sequence[_RobustnessRow],
    *,
    key: Callable[[_RobustnessRow], object],
    value: Callable[[_RobustnessRow], float],
) -> dict[object, float]:
    grouped: dict[object, list[float]] = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(value(row))
    return {
        group_key: statistics.fmean(values) for group_key, values in grouped.items()
    }


def _win_tie_loss(
    deltas: Sequence[float],
    direction: MetricDirection,
) -> tuple[int, int, int]:
    wins = ties = losses = 0
    for delta in deltas:
        directed = delta if direction == MetricDirection.HIGHER_IS_BETTER else -delta
        if math.isclose(directed, 0.0, abs_tol=_TIE_TOLERANCE):
            ties += 1
        elif directed > 0:
            wins += 1
        else:
            losses += 1
    return wins, ties, losses


def _clustered_bootstrap_interval(
    deltas: Sequence[float],
    *,
    bootstrap_samples: int,
    random_seed: int,
) -> ConfidenceIntervalV2:
    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    generator = random.Random(random_seed)
    sample_size = len(deltas)
    means = sorted(
        statistics.fmean(generator.choice(deltas) for _ in range(sample_size))
        for _ in range(bootstrap_samples)
    )
    return ConfidenceIntervalV2(
        lower=_percentile(means, 0.025),
        upper=_percentile(means, 0.975),
        bootstrap_samples=bootstrap_samples,
        random_seed=random_seed,
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _paired_effect_size(deltas: Sequence[float]) -> float | None:
    if len(deltas) < 2:
        return None
    deviation = statistics.stdev(deltas)
    if math.isclose(deviation, 0.0, abs_tol=_TIE_TOLERANCE):
        return None
    return statistics.fmean(deltas) / deviation


def _exact_two_sided_sign_test(wins: int, losses: int) -> float | None:
    discordant = wins + losses
    if discordant == 0:
        return None
    tail = sum(
        math.comb(discordant, index) for index in range(min(wins, losses) + 1)
    ) / (2**discordant)
    return min(1.0, 2 * tail)
