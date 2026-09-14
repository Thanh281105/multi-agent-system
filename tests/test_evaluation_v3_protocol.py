"""Package 7 evaluation v3 protocol hashing and global repeat tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_models import (
    PACKAGE7_PILOT_CASE_ORDER,
    PACKAGE7_VARIANT_ORDER,
    EvaluationAssetBindingsV3,
    EvaluationProtocolV3,
    ObservationIdentityV3,
    PilotTurnCostV3,
    ScheduledTurnKindV3,
    canonical_turn_id_v3,
)
from app.evaluation.v3_protocol import (
    PilotObservationCostEvidenceV3,
    PilotUserTurnCostEvidenceV3,
    RepeatDecisionBlockedV3,
    build_evaluation_protocol_v3,
    choose_global_repeat_decision_v3,
    evaluation_protocol_sha256_v3,
    load_evaluation_experiment_v3,
    validate_evaluation_protocol_v3,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_PATH = PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"


def test_protocol_binds_every_drift_sensitive_hash() -> None:
    loaded = load_evaluation_experiment_v3(EXPERIMENT_PATH)
    assets = _assets()
    protocol = build_evaluation_protocol_v3(loaded, assets=assets)

    assert protocol.experiment_sha256 == canonical_sha256(loaded.config)
    assert protocol.assets == assets
    assert protocol.case_bindings_sha256 == canonical_sha256(
        [case.model_dump(mode="json") for case in loaded.config.pilot_cases]
    )
    assert protocol.variant_set_sha256 == canonical_sha256(
        [variant.model_dump(mode="json") for variant in loaded.config.variants]
    )
    assert protocol.budget_sha256 == canonical_sha256(loaded.config.budget)
    assert len(protocol.schedule_algorithm_sha256) == 64
    assert len(protocol.repeat_rule_sha256) == 64
    validate_evaluation_protocol_v3(protocol)

    baseline_hash = evaluation_protocol_sha256_v3(protocol)
    for index, field_name in enumerate(EvaluationAssetBindingsV3.model_fields):
        changed = EvaluationAssetBindingsV3.model_validate(
            {
                **assets.model_dump(),
                field_name: f"{index + 20:064x}",
            }
        )
        changed_protocol = build_evaluation_protocol_v3(loaded, assets=changed)
        assert evaluation_protocol_sha256_v3(changed_protocol) != baseline_hash


def test_protocol_rejects_tampered_derived_hashes() -> None:
    protocol = _protocol()
    tampered = EvaluationProtocolV3.model_validate(
        {
            **protocol.model_dump(),
            "budget_sha256": "f" * 64,
        }
    )

    with pytest.raises(ValueError, match="budget_sha256"):
        validate_evaluation_protocol_v3(tampered)


@pytest.mark.parametrize(
    ("maximum_measured_cost", "expected_repeats", "expected_projection"),
    (
        (Decimal("0.09"), 3, Decimal("64.80")),
        (Decimal("0.10"), 2, Decimal("48.00")),
    ),
)
def test_global_repeat_rule_chooses_three_then_two_for_every_variant(
    maximum_measured_cost: Decimal,
    expected_repeats: int,
    expected_projection: Decimal,
) -> None:
    protocol = _protocol()
    costs = _pilot_costs(protocol, maximum_measured_cost)

    decision = choose_global_repeat_decision_v3(protocol, costs)
    reversed_decision = choose_global_repeat_decision_v3(
        protocol, tuple(reversed(costs))
    )

    assert decision.selected_repeats == expected_repeats
    assert decision.projected_benchmark_cost_usd == expected_projection
    assert tuple(item.variant_id for item in decision.per_variant) == (
        PACKAGE7_VARIANT_ORDER
    )
    assert {
        item.maximum_pilot_observation_cost_usd for item in decision.per_variant
    } == {maximum_measured_cost}
    assert decision.cost_evidence_sha256 == reversed_decision.cost_evidence_sha256


def test_repeat_rule_counts_unresolved_maxima_and_excludes_warmup_projection() -> None:
    protocol = _protocol()
    costs = _pilot_costs(
        protocol,
        Decimal("0.05"),
        unresolved_reservation=Decimal("0.05"),
        warmup_cost=Decimal("0.20"),
    )

    decision = choose_global_repeat_decision_v3(protocol, costs)

    assert decision.selected_repeats == 2
    assert decision.projected_benchmark_cost_usd == Decimal("48.00")
    assert decision.pilot_effective_cost_usd == Decimal("4.00")


def test_repeat_rule_validates_user_turn_costs_but_projects_observation_total() -> None:
    protocol = _protocol()
    costs: list[PilotTurnCostV3 | PilotObservationCostEvidenceV3] = list(
        _pilot_costs(protocol, Decimal("0.05"))
    )
    original = costs[4]
    assert isinstance(original, PilotTurnCostV3)
    aggregate = PilotTurnCostV3.model_validate(
        {
            **original.model_dump(mode="json"),
            "known_cost_usd": "0.40",
            "ledger_event_ids": ("ledger_multi_1", "ledger_multi_2"),
        }
    )
    costs[4] = PilotObservationCostEvidenceV3(
        pilot_cost=aggregate,
        user_turn_costs=(
            PilotUserTurnCostEvidenceV3(
                canonical_turn_id=aggregate.turn_id,
                execution_turn_id=f"eturn_{'1' * 64}",
                known_cost_usd=Decimal("0.20"),
                ledger_event_ids=("ledger_multi_1",),
            ),
            PilotUserTurnCostEvidenceV3(
                canonical_turn_id=aggregate.turn_id,
                execution_turn_id=f"eturn_{'2' * 64}",
                known_cost_usd=Decimal("0.20"),
                ledger_event_ids=("ledger_multi_2",),
            ),
        ),
    )

    decision = choose_global_repeat_decision_v3(protocol, costs)

    assert decision.selected_repeats == 2
    assert decision.pilot_effective_cost_usd == Decimal("1.99")
    assert decision.per_variant[0].maximum_pilot_observation_cost_usd == Decimal("0.40")
    assert decision.projected_benchmark_cost_usd == Decimal("66.00")

    costs[4] = aggregate
    with pytest.raises(RepeatDecisionBlockedV3, match="0.25 USD turn limit"):
        choose_global_repeat_decision_v3(protocol, costs)


def test_repeat_rule_blocks_over_budget_missing_cells_and_invalid_ledger() -> None:
    protocol = _protocol()
    with pytest.raises(RepeatDecisionBlockedV3, match="two global repeats"):
        choose_global_repeat_decision_v3(
            protocol,
            _pilot_costs(protocol, Decimal("0.15")),
        )

    complete = _pilot_costs(protocol, Decimal("0.05"))
    with pytest.raises(RepeatDecisionBlockedV3, match="8x4"):
        choose_global_repeat_decision_v3(protocol, complete[:-1])

    wrong_cell = list(complete)
    wrong_cell[-1] = _replace_cost_identity(
        wrong_cell[-1],
        case_id="dev_multi_constraint_01",
        work_group_id="dev_multi_constraint_01",
    )
    with pytest.raises(RepeatDecisionBlockedV3, match="duplicate turn IDs"):
        choose_global_repeat_decision_v3(protocol, wrong_cell)

    invalid = list(complete)
    invalid[4] = PilotTurnCostV3.model_validate(
        {**invalid[4].model_dump(), "ledger_valid": False}
    )
    with pytest.raises(RepeatDecisionBlockedV3, match="invalid ledger"):
        choose_global_repeat_decision_v3(protocol, invalid)


def test_repeat_rule_rejects_identity_protocol_group_run_and_ledger_tampering() -> None:
    protocol = _protocol()
    complete = _pilot_costs(protocol, Decimal("0.05"))

    wrong_protocol = list(complete)
    wrong_protocol[0] = _replace_cost_identity(
        wrong_protocol[0],
        protocol_sha256="f" * 64,
    )
    with pytest.raises(RepeatDecisionBlockedV3, match="protocol hash"):
        choose_global_repeat_decision_v3(protocol, wrong_protocol)

    wrong_group = list(complete)
    wrong_group[4] = _replace_cost_identity(
        wrong_group[4],
        work_group_id="dev_wrong_work_group",
    )
    with pytest.raises(RepeatDecisionBlockedV3, match="work group"):
        choose_global_repeat_decision_v3(protocol, wrong_group)

    mixed_run = list(complete)
    mixed_run[-1] = _replace_cost_identity(
        mixed_run[-1],
        run_id="run_package7_cost_gate_other",
    )
    with pytest.raises(RepeatDecisionBlockedV3, match="one run ID"):
        choose_global_repeat_decision_v3(protocol, mixed_run)

    reused_ledger = list(complete)
    reused_ledger[1] = PilotTurnCostV3.model_validate(
        {
            **reused_ledger[1].model_dump(),
            "ledger_event_ids": reused_ledger[0].ledger_event_ids,
        }
    )
    with pytest.raises(RepeatDecisionBlockedV3, match="reuses ledger event IDs"):
        choose_global_repeat_decision_v3(protocol, reused_ledger)


def _protocol() -> EvaluationProtocolV3:
    return build_evaluation_protocol_v3(
        load_evaluation_experiment_v3(EXPERIMENT_PATH),
        assets=_assets(),
    )


def _assets() -> EvaluationAssetBindingsV3:
    values = {
        field_name: f"{index + 1:064x}"
        for index, field_name in enumerate(EvaluationAssetBindingsV3.model_fields)
    }
    return EvaluationAssetBindingsV3.model_validate(values)


def _pilot_costs(
    protocol: EvaluationProtocolV3,
    measured_cost: Decimal,
    *,
    unresolved_reservation: Decimal = Decimal("0"),
    warmup_cost: Decimal = Decimal("0.01"),
) -> tuple[PilotTurnCostV3, ...]:
    costs: list[PilotTurnCostV3] = []
    protocol_hash = evaluation_protocol_sha256_v3(protocol)
    case_bindings = {
        case.case_id: case.work_group_id for case in protocol.experiment.pilot_cases
    }
    for variant_index, variant_id in enumerate(PACKAGE7_VARIANT_ORDER):
        identity = ObservationIdentityV3(
            run_id="run_package7_cost_gate",
            protocol_sha256=protocol_hash,
            schedule_algorithm_id="package7_pilot_interleaved_v1",
            turn_kind=ScheduledTurnKindV3.WARMUP,
            variant_id=variant_id,
            case_id=PACKAGE7_PILOT_CASE_ORDER[0],
            work_group_id=case_bindings[PACKAGE7_PILOT_CASE_ORDER[0]],
            repetition=0,
        )
        costs.append(
            PilotTurnCostV3(
                turn_id=canonical_turn_id_v3(identity),
                identity=identity,
                variant_id=variant_id,
                case_id=PACKAGE7_PILOT_CASE_ORDER[0],
                is_warmup=True,
                known_cost_usd=warmup_cost,
                ledger_attributed=True,
                ledger_valid=True,
                ledger_event_ids=(f"ledger_warm_{variant_index}",),
            )
        )
    unresolved = (
        (unresolved_reservation,) if unresolved_reservation > Decimal("0") else ()
    )
    for case_index, case_id in enumerate(PACKAGE7_PILOT_CASE_ORDER):
        for variant_index, variant_id in enumerate(PACKAGE7_VARIANT_ORDER):
            identity = ObservationIdentityV3(
                run_id="run_package7_cost_gate",
                protocol_sha256=protocol_hash,
                schedule_algorithm_id="package7_pilot_interleaved_v1",
                turn_kind=ScheduledTurnKindV3.MEASURED,
                variant_id=variant_id,
                case_id=case_id,
                work_group_id=case_bindings[case_id],
                repetition=0,
            )
            costs.append(
                PilotTurnCostV3(
                    turn_id=canonical_turn_id_v3(identity),
                    identity=identity,
                    variant_id=variant_id,
                    case_id=case_id,
                    is_warmup=False,
                    known_cost_usd=measured_cost,
                    unresolved_reservation_maxima_usd=unresolved,
                    ledger_attributed=True,
                    ledger_valid=True,
                    ledger_event_ids=(f"ledger_measured_{case_index}_{variant_index}",),
                )
            )
    return tuple(costs)


def _replace_cost_identity(
    cost: PilotTurnCostV3,
    **updates: object,
) -> PilotTurnCostV3:
    identity = ObservationIdentityV3.model_validate(
        {**cost.identity.model_dump(), **updates}
    )
    return PilotTurnCostV3.model_validate(
        {
            **cost.model_dump(),
            "turn_id": canonical_turn_id_v3(identity),
            "identity": identity.model_dump(),
            "variant_id": identity.variant_id,
            "case_id": identity.case_id,
            "is_warmup": identity.turn_kind == ScheduledTurnKindV3.WARMUP,
        }
    )
