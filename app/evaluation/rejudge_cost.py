"""Tính chi phí và ép trần ngân sách cho việc chấm lại bằng LLM judge."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_MILLION = Decimal(1_000_000)


class RejudgeCostError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelPrices:
    input_per_m: Decimal
    cached_input_per_m: Decimal
    output_per_m: Decimal


def load_model_prices(manifest_path: Path, model: str) -> ModelPrices:
    models = json.loads(manifest_path.read_text(encoding="utf-8")).get("models", {})
    entry = models.get(model)
    if entry is None:
        raise RejudgeCostError(f"pricing manifest không có giá cho model {model!r}")
    return ModelPrices(
        Decimal(str(entry["input_usd_per_million"])),
        Decimal(str(entry["cached_input_usd_per_million"])),
        Decimal(str(entry["output_usd_per_million"])),
    )


def call_cost_usd(
    prices: ModelPrices,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> Decimal:
    fresh = max(input_tokens - cached_input_tokens, 0)
    return (
        Decimal(fresh) * prices.input_per_m
        + Decimal(cached_input_tokens) * prices.cached_input_per_m
        + Decimal(output_tokens) * prices.output_per_m
    ) / _MILLION


class BudgetGuard:
    def __init__(self, limit_usd: Decimal) -> None:
        self.limit_usd = limit_usd
        self.spent = Decimal(0)

    def charge(self, cost: Decimal) -> None:
        self.spent += cost

    def would_exceed(self, next_max_cost: Decimal) -> bool:
        return self.spent + next_max_cost > self.limit_usd
