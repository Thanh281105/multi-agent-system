"""Reproduce conservative planning power for the paired binary primary metric."""

from __future__ import annotations

import argparse
import json
from math import ceil, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any


def paired_binary_power_plan(
    *,
    discordance: float,
    true_effect: float = 0.10,
    mcid: float = 0.05,
    alpha_two_sided: float = 0.05,
    target_power: float = 0.80,
    candidate_groups: int = 628,
) -> dict[str, Any]:
    if not 0 < discordance <= 1:
        raise ValueError("discordance must be in (0, 1]")
    if not 0 <= mcid < true_effect <= 1:
        raise ValueError("require 0 <= MCID < true effect <= 1")
    if not 0 < alpha_two_sided < 1 or not 0 < target_power < 1:
        raise ValueError("alpha and target power must be in (0, 1)")

    normal = NormalDist()
    z_alpha = normal.inv_cdf(1 - alpha_two_sided / 2)
    z_power = normal.inv_cdf(target_power)
    effect_over_mcid = true_effect - mcid
    required_groups = ceil(
        discordance * (z_alpha + z_power) ** 2 / effect_over_mcid**2
    )
    achieved_power = normal.cdf(
        effect_over_mcid * sqrt(candidate_groups / discordance) - z_alpha
    )
    return {
        "method": "conservative_normal_approximation_for_paired_binary_difference",
        "analysis_unit": "independent_task_group",
        "discordance_probability_D": discordance,
        "true_effect": true_effect,
        "mcid": mcid,
        "alpha_two_sided": alpha_two_sided,
        "target_power": target_power,
        "candidate_groups": candidate_groups,
        "required_independent_groups": required_groups,
        "candidate_power_under_assumptions": achieved_power,
        "variance_bound": "Var(paired difference) <= D; no arm-cell inflation",
        "caveat": (
            "Planning approximation only. Estimate D on development data before lock; "
            "report the registered task-group bootstrap confidence interval "
            "on validation."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-groups", type=int, default=628)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    rows = [
        paired_binary_power_plan(
            discordance=discordance,
            candidate_groups=arguments.candidate_groups,
        )
        for discordance in (0.10, 0.20, 0.30, 0.50)
    ]
    report = json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
