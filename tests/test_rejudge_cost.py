from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation.rejudge_cost import (
    BudgetGuard,
    ModelPrices,
    RejudgeCostError,
    call_cost_usd,
    load_model_prices,
)

MANIFEST = (
    Path(__file__).resolve().parents[1] / "app" / "shared" / "pricing-manifest.json"
)


def test_load_luna_prices_from_repo_manifest() -> None:
    prices = load_model_prices(MANIFEST, "gpt-6-luna")
    assert prices == ModelPrices(Decimal("0.10"), Decimal("0.01"), Decimal("0.50"))


def test_call_cost_splits_cached() -> None:
    prices = load_model_prices(MANIFEST, "gpt-6-luna")
    cost = call_cost_usd(
        prices,
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        output_tokens=100_000,
    )
    assert cost == Decimal("0.114")


def test_missing_model_raises() -> None:
    with pytest.raises(RejudgeCostError):
        load_model_prices(MANIFEST, "khong-ton-tai")


def test_budget_cap_stops_before_next_call() -> None:
    guard = BudgetGuard(Decimal("1.00"))
    guard.charge(Decimal("0.90"))
    assert guard.spent == Decimal("0.90")
    assert guard.would_exceed(Decimal("0.20")) is True
    assert guard.would_exceed(Decimal("0.10")) is False
