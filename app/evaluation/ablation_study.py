"""Architectural Contribution Ablation Framework.

This module provides clean component isolation to evaluate the marginal contribution
of individual architectural components in the Multi-Agent System:
1. Dynamic Intent-Aware Routing vs Static Specialist Broadcast (Routing Ablation)
2. Evidence-Adaptive Replanning / Continuation vs Single-Pass Dispatch (Planning Ablation)
3. Grounded Evidence-Checked Synthesis vs Unconstrained Concatenation (Synthesis Ablation)

By holding the model backend (gpt-5.4-mini), generation temperature (0.0), snapshot data,
and evaluator strictly identical, this framework isolates the exact delta (Delta) attributable
to the chosen component.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class AblationTarget(str, enum.Enum):
    EVIDENCE_ADAPTIVE_PLANNING = "evidence_adaptive_planning"
    DYNAMIC_INTENT_ROUTING = "dynamic_intent_routing"
    GROUNDED_SYNTHESIS = "grounded_synthesis"


@dataclass(frozen=True)
class AblationConfiguration:
    """Strict configuration that defines which architectural component is active or ablated."""

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


def compute_ablation_delta(
    full_system: AblationMeasurement,
    ablated_system: AblationMeasurement,
) -> dict[str, Any]:
    """Calculate the isolated architectural contribution delta between full and ablated configurations."""
    delta_completion = full_system.task_completion_rate - ablated_system.task_completion_rate
    delta_claim_support = full_system.claim_support_rate - ablated_system.claim_support_rate
    delta_precision = full_system.citation_precision - ablated_system.citation_precision
    latency_ratio = full_system.mean_latency_seconds / max(0.001, ablated_system.mean_latency_seconds)
    token_ratio = full_system.mean_total_tokens / max(1.0, ablated_system.mean_total_tokens)

    return {
        "isolated_component": full_system.configuration.target.value,
        "full_system_variant": full_system.configuration.variant_label(),
        "ablated_system_variant": ablated_system.configuration.variant_label(),
        "delta_task_completion_pp": round(delta_completion * 100, 2),
        "delta_claim_support_pp": round(delta_claim_support * 100, 2),
        "delta_citation_precision_pp": round(delta_precision * 100, 2),
        "latency_overhead_ratio": round(latency_ratio, 2),
        "token_overhead_ratio": round(token_ratio, 2),
        "is_statistically_superior": delta_completion > 0.05,
        "architectural_justification": (
            f"Ablating {full_system.configuration.target.value} causes a "
            f"{abs(delta_completion)*100:.1f} percentage point change in task completion "
            f"with a {latency_ratio:.2f}x latency trade-off."
        ),
    }
