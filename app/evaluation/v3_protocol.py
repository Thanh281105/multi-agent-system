"""Load, freeze, hash, and cost-gate the Package 7 evaluation protocol."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, NoReturn, Sequence

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_models import (
    PACKAGE7_VARIANT_ORDER,
    EvaluationAssetBindingsV3,
    EvaluationExperimentConfigV3,
    EvaluationProtocolV3,
    PilotTurnCostV3,
    RepeatDecisionV3,
    VariantCostProjectionV3,
)

REPEAT_RULE_ID_V3: Literal["package7_global_cost_gate_v1"] = (
    "package7_global_cost_gate_v1"
)


@dataclass(frozen=True, slots=True)
class LoadedEvaluationExperimentV3:
    config: EvaluationExperimentConfigV3
    experiment_sha256: str


class RepeatDecisionBlockedV3(RuntimeError):
    """Raised when the frozen global repeat rule cannot make a safe decision."""


def load_evaluation_experiment_v3(path: Path) -> LoadedEvaluationExperimentV3:
    config = EvaluationExperimentConfigV3.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    return LoadedEvaluationExperimentV3(
        config=config,
        experiment_sha256=canonical_sha256(config),
    )


def build_evaluation_protocol_v3(
    loaded: LoadedEvaluationExperimentV3,
    *,
    assets: EvaluationAssetBindingsV3,
) -> EvaluationProtocolV3:
    from app.evaluation.v3_schedule import (
        SCHEDULE_ALGORITHM_ID_V3,
        schedule_algorithm_sha256_v3,
    )

    config = loaded.config
    protocol = EvaluationProtocolV3(
        protocol_id=config.experiment_id,
        experiment_sha256=loaded.experiment_sha256,
        experiment=config,
        assets=assets,
        case_bindings_sha256=_case_bindings_sha256(config),
        variant_set_sha256=_variant_set_sha256(config),
        budget_sha256=canonical_sha256(config.budget),
        schedule_algorithm_id=SCHEDULE_ALGORITHM_ID_V3,
        schedule_algorithm_sha256=schedule_algorithm_sha256_v3(),
        repeat_rule_id=REPEAT_RULE_ID_V3,
        repeat_rule_sha256=_repeat_rule_sha256(config),
    )
    validate_evaluation_protocol_v3(protocol)
    return protocol


def validate_evaluation_protocol_v3(protocol: EvaluationProtocolV3) -> None:
    from app.evaluation.v3_schedule import schedule_algorithm_sha256_v3

    config = protocol.experiment
    expected = {
        "experiment_sha256": canonical_sha256(config),
        "case_bindings_sha256": _case_bindings_sha256(config),
        "variant_set_sha256": _variant_set_sha256(config),
        "budget_sha256": canonical_sha256(config.budget),
        "schedule_algorithm_sha256": schedule_algorithm_sha256_v3(),
        "repeat_rule_sha256": _repeat_rule_sha256(config),
    }
    actual = {
        "experiment_sha256": protocol.experiment_sha256,
        "case_bindings_sha256": protocol.case_bindings_sha256,
        "variant_set_sha256": protocol.variant_set_sha256,
        "budget_sha256": protocol.budget_sha256,
        "schedule_algorithm_sha256": protocol.schedule_algorithm_sha256,
        "repeat_rule_sha256": protocol.repeat_rule_sha256,
    }
    mismatches = sorted(key for key in expected if expected[key] != actual[key])
    if mismatches:
        raise ValueError(f"evaluation v3 protocol hash mismatch: {mismatches}")


def evaluation_protocol_sha256_v3(protocol: EvaluationProtocolV3) -> str:
    validate_evaluation_protocol_v3(protocol)
    return canonical_sha256(protocol)


def choose_global_repeat_decision_v3(
    protocol: EvaluationProtocolV3,
    costs: Sequence[PilotTurnCostV3],
) -> RepeatDecisionV3:
    """Apply the frozen Package 7 pilot-cost rule once for all variants."""

    validate_evaluation_protocol_v3(protocol)
    _validate_pilot_cost_evidence(protocol, costs)
    pilot_cost = sum((item.effective_cost_usd for item in costs), Decimal("0"))
    if pilot_cost > protocol.experiment.budget.pilot_allocation_usd:
        _block("pilot effective cost exceeds the 10 USD pilot allocation")

    measured = tuple(item for item in costs if not item.is_warmup)
    projections = tuple(
        VariantCostProjectionV3(
            variant_id=variant_id,
            maximum_pilot_observation_cost_usd=max(
                item.effective_cost_usd
                for item in measured
                if item.variant_id == variant_id
            ),
        )
        for variant_id in PACKAGE7_VARIANT_ORDER
    )
    per_repeat_cost = sum(
        (
            item.maximum_pilot_observation_cost_usd
            * protocol.experiment.repeat_policy.heldout_conversation_count
            for item in projections
        ),
        Decimal("0"),
    )
    selected_repeats: Literal[2, 3]
    projected_cost: Decimal
    projected_three = per_repeat_cost * 3
    if projected_three <= protocol.experiment.repeat_policy.benchmark_limit_usd:
        selected_repeats = 3
        projected_cost = projected_three
    else:
        projected_two = per_repeat_cost * 2
        if projected_two > protocol.experiment.repeat_policy.benchmark_limit_usd:
            _block("two global repeats exceed the 70 USD benchmark allocation")
        selected_repeats = 2
        projected_cost = projected_two

    ordered_costs = sorted(
        (item.model_dump(mode="json") for item in costs),
        key=lambda item: str(item["turn_id"]),
    )
    return RepeatDecisionV3(
        protocol_sha256=evaluation_protocol_sha256_v3(protocol),
        repeat_rule_sha256=protocol.repeat_rule_sha256,
        cost_evidence_sha256=canonical_sha256(ordered_costs),
        selected_repeats=selected_repeats,
        pilot_effective_cost_usd=pilot_cost,
        projected_benchmark_cost_usd=projected_cost,
        per_variant=projections,
    )


def _validate_pilot_cost_evidence(
    protocol: EvaluationProtocolV3,
    costs: Sequence[PilotTurnCostV3],
) -> None:
    protocol_hash = evaluation_protocol_sha256_v3(protocol)
    if any(item.identity.protocol_sha256 != protocol_hash for item in costs):
        _block("pilot cost identity protocol hash does not match the frozen protocol")
    run_ids = {item.identity.run_id for item in costs}
    if len(run_ids) != 1:
        _block("pilot cost evidence must belong to one run ID")
    if any(item.identity.repetition != 0 for item in costs):
        _block("pilot cost identities must use repetition zero")
    if any(not item.ledger_attributed or not item.ledger_valid for item in costs):
        _block("pilot contains unattributed or invalid ledger evidence")
    ledger_event_ids = [
        event_id for item in costs for event_id in item.ledger_event_ids
    ]
    if len(ledger_event_ids) != len(set(ledger_event_ids)):
        _block("pilot cost evidence reuses ledger event IDs across turns")
    turn_ids = [item.turn_id for item in costs]
    if len(turn_ids) != len(set(turn_ids)):
        _block("pilot cost evidence contains duplicate turn IDs")
    if any(
        item.effective_cost_usd > protocol.experiment.budget.per_turn_limit_usd
        for item in costs
    ):
        _block("pilot turn exceeds the frozen 0.25 USD turn limit")

    case_bindings: dict[str, str] = {
        case.case_id: case.work_group_id for case in protocol.experiment.pilot_cases
    }
    if any(
        case_bindings.get(item.identity.case_id) != item.identity.work_group_id
        for item in costs
    ):
        _block("pilot cost identity work group does not match the configured case")

    warmup_work_group = case_bindings[protocol.experiment.warmup_case_id]
    warmup_keys = {
        (
            item.identity.variant_id,
            item.identity.case_id,
            item.identity.work_group_id,
            item.identity.repetition,
        )
        for item in costs
        if item.is_warmup
    }
    expected_warmup_keys = {
        (
            variant_id,
            protocol.experiment.warmup_case_id,
            warmup_work_group,
            0,
        )
        for variant_id in PACKAGE7_VARIANT_ORDER
    }
    if warmup_keys != expected_warmup_keys or sum(
        item.is_warmup for item in costs
    ) != len(expected_warmup_keys):
        _block("pilot requires exactly one attributed warmup per variant")

    measured_keys = {
        (
            item.identity.variant_id,
            item.identity.case_id,
            item.identity.work_group_id,
            item.identity.repetition,
        )
        for item in costs
        if not item.is_warmup
    }
    expected_measured_keys = {
        (variant_id, case.case_id, case.work_group_id, 0)
        for case in protocol.experiment.pilot_cases
        for variant_id in PACKAGE7_VARIANT_ORDER
    }
    if measured_keys != expected_measured_keys or sum(
        not item.is_warmup for item in costs
    ) != len(expected_measured_keys):
        _block("pilot cost evidence must cover every 8x4 measured cell once")


def _case_bindings_sha256(config: EvaluationExperimentConfigV3) -> str:
    return canonical_sha256(
        [case.model_dump(mode="json") for case in config.pilot_cases]
    )


def _variant_set_sha256(config: EvaluationExperimentConfigV3) -> str:
    return canonical_sha256(
        [variant.model_dump(mode="json") for variant in config.variants]
    )


def _repeat_rule_sha256(config: EvaluationExperimentConfigV3) -> str:
    descriptor = {
        "rule_id": REPEAT_RULE_ID_V3,
        "policy": config.repeat_policy.model_dump(mode="json"),
        "pilot_coverage": "eight_cases_by_four_variants_once_plus_warmup",
        "effective_observation_cost": (
            "known_cost_plus_each_unresolved_reservation_at_reserved_maximum"
        ),
        "variant_projection_basis": "maximum_measured_pilot_observation_cost",
        "projection_formula": (
            "sum(per_variant_maximum * 60_heldout_conversations * repeats)"
        ),
        "decision_order": [3, 2, "block"],
    }
    return canonical_sha256(descriptor)


def _block(message: str) -> NoReturn:
    raise RepeatDecisionBlockedV3(message)
