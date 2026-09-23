"""Frozen Package 7 evaluation v3 contract tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts import TaskStatus
from app.evaluation.v3_models import (
    PACKAGE7_METRICS,
    PACKAGE7_PILOT_CASE_ORDER,
    PACKAGE7_VARIANT_ORDER,
    BudgetPolicyV3,
    EvaluationExperimentConfigV3,
    EvaluationMetricV3,
    EvaluationObservationV3,
    GoldAuthorshipV3,
    GoldPolicyV3,
    JudgmentModeV3,
    JudgmentPolicyV3,
    ObservationIdentityV3,
    PilotCaseBindingV3,
    PilotTurnCostV3,
    ScheduledTurnKindV3,
    canonical_observation_id_v3,
    canonical_turn_id_v3,
)
from app.evaluation.v3_protocol import load_evaluation_experiment_v3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_PATH = PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"


def test_experiment_freezes_variants_cases_models_metrics_and_labels() -> None:
    config = load_evaluation_experiment_v3(EXPERIMENT_PATH).config

    assert tuple(variant.variant_id for variant in config.variants) == (
        PACKAGE7_VARIANT_ORDER
    )
    assert tuple(case.case_id for case in config.pilot_cases) == (
        PACKAGE7_PILOT_CASE_ORDER
    )
    assert tuple(case.work_group_id for case in config.pilot_cases) == (
        "work_sapiens_yuval_noah_harari",
        "work_de_men_phieu_luu_ky_to_hoai",
        "work_sapiens_yuval_noah_harari",
        "work_de_men_phieu_luu_ky_to_hoai",
        "pair_sapiens_social_contract",
        "work_sapiens_yuval_noah_harari",
        "unbound_exact_title_price_probe",
        "pair_sapiens_zero_to_one",
    )
    assert config.pilot_repeats == config.warmup_repeats == 1
    assert {variant.generation_binding.model for variant in config.variants} == {
        "gpt-5.4-mini-2026-03-17"
    }
    assert {
        variant.generation_binding.reasoning_effort for variant in config.variants
    } == {"low"}
    assert config.variants[2].rag_enabled is True
    assert config.variants[3].rag_enabled is False
    assert config.metrics == PACKAGE7_METRICS
    assert "exact_plan" not in {metric.value for metric in EvaluationMetricV3}
    assert "exact_plan" not in EvaluationObservationV3.model_fields
    assert config.gold_policy.authorship == GoldAuthorshipV3.AUTOMATED_PRE_SUT_SPEC
    assert config.judgment_policy.mode == JudgmentModeV3.MODEL_JUDGE
    assert config.gold_policy.human_author_ids == ()
    assert config.judgment_policy.human_judge_ids == ()


def test_model_alias_and_variant_drift_are_rejected(tmp_path: Path) -> None:
    payload = json.loads(EXPERIMENT_PATH.read_text(encoding="utf-8"))
    payload["variants"][0]["generation_binding"]["model"] = "gpt-5.4-mini"
    alias_path = tmp_path / "alias.json"
    alias_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_evaluation_experiment_v3(alias_path)

    payload = json.loads(EXPERIMENT_PATH.read_text(encoding="utf-8"))
    payload["variants"][1]["max_continuations"] = 1
    with pytest.raises(ValidationError, match="does not match Package 7"):
        EvaluationExperimentConfigV3.model_validate(payload)

    payload = json.loads(EXPERIMENT_PATH.read_text(encoding="utf-8"))
    payload["variants"] = list(reversed(payload["variants"]))
    with pytest.raises(ValidationError, match="frozen order"):
        EvaluationExperimentConfigV3.model_validate(payload)


def test_budget_is_frozen_and_finite() -> None:
    budget = BudgetPolicyV3()

    assert budget.corpus_allocation_usd == Decimal("5")
    assert budget.pilot_allocation_usd == Decimal("10")
    assert budget.benchmark_allocation_usd == Decimal("70")
    assert budget.reserve_allocation_usd == Decimal("15")
    assert budget.account_warning_usd == Decimal("50")
    assert budget.reservation_cap_usd == Decimal("100")
    assert budget.per_turn_limit_usd == Decimal("0.25")
    assert budget.max_generation_calls_per_turn == 10
    assert budget.max_provider_attempts_per_turn == 16
    assert budget.provider_concurrency == 2
    assert budget.max_retries == 1
    assert budget.attempt_timeout_seconds == 18
    assert budget.turn_deadline_seconds == 60
    assert budget.max_input_tokens_per_generation == 12_000
    assert budget.max_output_tokens_per_generation == 1_200

    with pytest.raises(ValidationError, match="monetary allocations"):
        BudgetPolicyV3(pilot_allocation_usd=Decimal("11"))
    with pytest.raises(ValidationError):
        BudgetPolicyV3(per_turn_limit_usd=Decimal("NaN"))


def test_human_labels_require_explicit_human_ids_and_secrets_are_forbidden() -> None:
    with pytest.raises(ValidationError, match="explicit human author IDs"):
        GoldPolicyV3(authorship=GoldAuthorshipV3.HUMAN_PRE_SUT_SPEC)
    with pytest.raises(ValidationError, match="explicit human judge IDs"):
        JudgmentPolicyV3(mode=JudgmentModeV3.HUMAN_REVIEW)

    payload = json.loads(EXPERIMENT_PATH.read_text(encoding="utf-8"))
    payload["api_key"] = "must-never-serialize"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvaluationExperimentConfigV3.model_validate(payload)

    serialized = load_evaluation_experiment_v3(EXPERIMENT_PATH).config.model_dump_json()
    lowered = serialized.lower()
    assert "api_key" not in lowered
    assert "credential" not in lowered
    assert "secret" not in lowered


def test_runtime_case_ids_accept_heldout_ids_but_reject_invalid_identifiers() -> None:
    heldout_case_id = "heldout_multi_constraint_01"
    identity = ObservationIdentityV3(
        run_id="run_package8_benchmark",
        protocol_sha256="a" * 64,
        schedule_algorithm_id="package7_pilot_interleaved_v1",
        turn_kind=ScheduledTurnKindV3.MEASURED,
        variant_id="ma_adaptive_rag",
        case_id=heldout_case_id,
        work_group_id="heldout_work_01",
        repetition=0,
    )
    observation = EvaluationObservationV3(
        observation_id=canonical_observation_id_v3(identity),
        identity=identity,
        run_id="run_package8_benchmark",
        protocol_sha256="a" * 64,
        variant_id="ma_adaptive_rag",
        case_id=heldout_case_id,
        work_group_id="heldout_work_01",
        repetition=0,
        execution_order=0,
        status=TaskStatus.SUCCESS,
        task_completed=True,
        answerable=True,
        abstained=False,
        authorized=True,
        valid_plan=True,
        duplicate_dispatch_count=0,
        end_to_end_latency_ms=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        known_cost_usd=Decimal("0.01"),
        unresolved_reserved_cost_usd=Decimal("0"),
        ledger_event_ids=("ledger_package8_heldout_01",),
    )
    cost = PilotTurnCostV3(
        turn_id=canonical_turn_id_v3(identity),
        identity=identity,
        variant_id="ma_adaptive_rag",
        case_id=heldout_case_id,
        is_warmup=False,
        known_cost_usd=Decimal("0.01"),
        ledger_attributed=True,
        ledger_valid=True,
        ledger_event_ids=("ledger_package8_heldout_cost_01",),
    )

    assert identity.case_id == observation.case_id == cost.case_id == heldout_case_id

    with pytest.raises(ValidationError):
        ObservationIdentityV3.model_validate(
            {**identity.model_dump(), "case_id": "Held out case!"}
        )
    with pytest.raises(ValidationError):
        PilotTurnCostV3.model_validate({**cost.model_dump(), "case_id": "../heldout"})


def test_observation_and_cost_reject_identity_and_ledger_tampering() -> None:
    identity = ObservationIdentityV3(
        run_id="run_package7_integrity",
        protocol_sha256="b" * 64,
        schedule_algorithm_id="package7_pilot_interleaved_v1",
        turn_kind=ScheduledTurnKindV3.MEASURED,
        variant_id="ma_adaptive_rag",
        case_id="dev_multi_constraint_01",
        work_group_id="dev_multi_constraint_01",
        repetition=0,
    )
    observation_payload = {
        "observation_id": canonical_observation_id_v3(identity),
        "identity": identity.model_dump(),
        "run_id": identity.run_id,
        "protocol_sha256": identity.protocol_sha256,
        "variant_id": identity.variant_id,
        "case_id": identity.case_id,
        "work_group_id": identity.work_group_id,
        "repetition": identity.repetition,
        "execution_order": 0,
        "status": TaskStatus.SUCCESS,
        "task_completed": True,
        "answerable": True,
        "abstained": False,
        "authorized": True,
        "valid_plan": True,
        "duplicate_dispatch_count": 0,
        "end_to_end_latency_ms": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "known_cost_usd": Decimal("0.01"),
        "unresolved_reserved_cost_usd": Decimal("0"),
        "ledger_event_ids": ["ledger_package7_observation_01"],
    }
    EvaluationObservationV3.model_validate(observation_payload)

    with pytest.raises(ValidationError, match="observation ID"):
        EvaluationObservationV3.model_validate(
            {**observation_payload, "observation_id": "obs_tampered_identity"}
        )
    with pytest.raises(ValidationError, match="fields do not match"):
        EvaluationObservationV3.model_validate(
            {**observation_payload, "case_id": "dev_multi_constraint_02"}
        )
    warmup_identity = identity.model_copy(
        update={"turn_kind": ScheduledTurnKindV3.WARMUP}
    )
    with pytest.raises(ValidationError, match="measured identity"):
        EvaluationObservationV3.model_validate(
            {
                **observation_payload,
                "identity": warmup_identity.model_dump(),
                "observation_id": canonical_observation_id_v3(warmup_identity),
            }
        )

    cost_payload = {
        "turn_id": canonical_turn_id_v3(identity),
        "identity": identity.model_dump(),
        "variant_id": identity.variant_id,
        "case_id": identity.case_id,
        "is_warmup": False,
        "known_cost_usd": Decimal("0.01"),
        "ledger_attributed": True,
        "ledger_valid": True,
        "ledger_event_ids": ["ledger_package7_cost_01"],
    }
    PilotTurnCostV3.model_validate(cost_payload)
    with pytest.raises(ValidationError, match="turn ID"):
        PilotTurnCostV3.model_validate(
            {**cost_payload, "turn_id": "turn_tampered_identity"}
        )
    with pytest.raises(ValidationError, match="fields do not match"):
        PilotTurnCostV3.model_validate(
            {**cost_payload, "case_id": "dev_multi_constraint_02"}
        )
    with pytest.raises(ValidationError, match="warmup label"):
        PilotTurnCostV3.model_validate({**cost_payload, "is_warmup": True})
    with pytest.raises(ValidationError, match="at least 1 item"):
        PilotTurnCostV3.model_validate({**cost_payload, "ledger_event_ids": []})
    with pytest.raises(ValidationError, match="must be unique"):
        PilotTurnCostV3.model_validate(
            {
                **cost_payload,
                "ledger_event_ids": ["ledger_duplicate", "ledger_duplicate"],
            }
        )


def test_pilot_case_binding_still_rejects_non_frozen_case_ids() -> None:
    with pytest.raises(ValidationError):
        PilotCaseBindingV3(
            case_id="heldout_multi_constraint_01",  # type: ignore[arg-type]
            work_group_id="heldout_work_01",
        )
