"""Descriptive ablation arithmetic; experimental control is the caller's duty.

Aggregate measurements alone cannot establish causal attribution or statistical
superiority. Use the frozen evaluation harness for paired observations and CIs.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class AblationTarget(str, enum.Enum):
    EVIDENCE_ADAPTIVE_PLANNING = "evidence_adaptive_planning"
    DYNAMIC_INTENT_ROUTING = "dynamic_intent_routing"
    GROUNDED_SYNTHESIS = "grounded_synthesis"


@dataclass(frozen=True)
class AblationConfiguration:
    """Declare which component is active or ablated."""

    target: AblationTarget
    is_ablated: bool
    description: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def variant_label(self, base_variant: str = "ma_adaptive_rag") -> str:
        if not self.is_ablated:
            return f"{base_variant}_full"
        return f"{base_variant}_ablate_{self.target.value}"


@dataclass(frozen=True)
class AblationMeasurement:
    """Quantitative measurement of an evaluation run under an ablation configuration."""

    configuration: AblationConfiguration
    task_count: int
    task_completion_rate: float
    claim_support_rate: float
    citation_precision: float
    citation_coverage: float
    mean_latency_seconds: float
    p95_latency_seconds: float
    mean_total_tokens: float
    mean_cost_usd: float
    tier_breakdown: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.task_count) is not int or self.task_count <= 0:
            raise ValueError("task_count must be a positive integer")
        for name in (
            "task_completion_rate",
            "claim_support_rate",
            "citation_precision",
            "citation_coverage",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be finite and between zero and one")
        for name in (
            "mean_latency_seconds",
            "p95_latency_seconds",
            "mean_total_tokens",
            "mean_cost_usd",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if any(
            isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1
            for value in self.tier_breakdown.values()
        ):
            raise ValueError("tier rates must be finite and between zero and one")


def compute_ablation_delta(
    full_system: AblationMeasurement,
    ablated_system: AblationMeasurement,
) -> dict[str, Any]:
    """Describe two measurements without inferring significance from effect size."""
    if full_system.configuration.target != ablated_system.configuration.target:
        raise ValueError("ablation measurements must describe the same component")
    if (
        full_system.configuration.is_ablated
        or not ablated_system.configuration.is_ablated
    ):
        raise ValueError("expected a full configuration followed by an ablated one")
    if full_system.task_count != ablated_system.task_count:
        raise ValueError("ablation measurements must have the same task count")
    delta_completion = (
        full_system.task_completion_rate - ablated_system.task_completion_rate
    )
    delta_claim_support = (
        full_system.claim_support_rate - ablated_system.claim_support_rate
    )
    delta_precision = full_system.citation_precision - ablated_system.citation_precision
    latency_ratio = (
        full_system.mean_latency_seconds / ablated_system.mean_latency_seconds
        if ablated_system.mean_latency_seconds > 0
        else None
    )
    token_ratio = (
        full_system.mean_total_tokens / ablated_system.mean_total_tokens
        if ablated_system.mean_total_tokens > 0
        else None
    )

    return {
        "component": full_system.configuration.target.value,
        "full_system_variant": full_system.configuration.variant_label(),
        "ablated_system_variant": ablated_system.configuration.variant_label(),
        "delta_task_completion_pp": round(delta_completion * 100, 2),
        "delta_claim_support_pp": round(delta_claim_support * 100, 2),
        "delta_citation_precision_pp": round(delta_precision * 100, 2),
        "latency_overhead_ratio": (
            round(latency_ratio, 2) if latency_ratio is not None else None
        ),
        "token_overhead_ratio": (
            round(token_ratio, 2) if token_ratio is not None else None
        ),
        "is_statistically_superior": None,
        "inference_status": "not_tested",
        "attribution_status": "requires_controlled_paired_observations",
    }
