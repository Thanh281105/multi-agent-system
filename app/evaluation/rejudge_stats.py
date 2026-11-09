"""Thống kê đồng thuận và so sánh ghép cặp cho việc chấm lại bằng LLM judge."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PairedDelta:
    mean_delta: float
    ci_low: float
    ci_high: float
    wins: int
    ties: int
    losses: int


def agreement(a: Sequence[object], b: Sequence[object]) -> float | None:
    if not a:
        return None
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def cohens_kappa(a: Sequence[object], b: Sequence[object]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[label] * cb[label] for label in ca) / (n * n)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def paired_variant_delta(
    scores: Mapping[tuple[str, str], float],
    *,
    baseline: str,
    treatment: str,
    seed: int = 42,
    resamples: int = 10_000,
) -> PairedDelta:
    base_groups = {g for g, v in scores if v == baseline}
    treat_groups = {g for g, v in scores if v == treatment}
    groups = sorted(base_groups & treat_groups)
    deltas = [scores[(g, treatment)] - scores[(g, baseline)] for g in groups]
    if not deltas:
        raise ValueError("không có work group chung giữa hai biến thể")
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(rng.choices(deltas, k=n)) / n for _ in range(resamples))
    low = means[int(0.025 * (resamples - 1))]
    high = means[int(0.975 * (resamples - 1))]
    return PairedDelta(
        mean_delta=sum(deltas) / n,
        ci_low=low,
        ci_high=high,
        wins=sum(d > 0 for d in deltas),
        ties=sum(d == 0 for d in deltas),
        losses=sum(d < 0 for d in deltas),
    )
