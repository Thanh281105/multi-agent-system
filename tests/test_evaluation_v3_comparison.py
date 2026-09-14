"""Grouped statistics and strict completeness tests for Evaluation v3."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts import TaskStatus
from app.evaluation.v3_comparison import (
    EvaluationStopV3,
    analyze_evaluation_v3,
    compare_variants_v3,
)
from app.evaluation.v3_gold import (
    EvaluationSplitManifestV3,
    EvaluationSplitV3,
    load_evaluation_gold_v3,
)
from app.evaluation.v3_models import (
    PACKAGE7_VARIANT_ORDER,
    EvaluationAssetBindingsV3,
    EvaluationMetricV3,
    EvaluationObservationV3,
    EvaluationProtocolV3,
    ObservationIdentityV3,
    RepeatDecisionV3,
    VariantCostProjectionV3,
    VariantIdV3,
    canonical_observation_id_v3,
)
from app.evaluation.v3_protocol import (
    build_evaluation_protocol_v3,
    evaluation_protocol_sha256_v3,
    load_evaluation_experiment_v3,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_SHA256 = "a" * 64


def test_grouping_nests_repetitions_and_paraphrases_without_pseudoreplication() -> None:
    observations: list[EvaluationObservationV3] = []
    execution_order = 0
    for work_group_id, case_ids, candidate_success in (
        ("work_positive", ("case_positive_a", "case_positive_b"), True),
        ("work_negative", ("case_negative",), False),
    ):
        for case_id in case_ids:
            for repetition in range(2):
                observations.append(
                    _observation(
                        variant_id="sa_shared_tools_rag",
                        case_id=case_id,
                        work_group_id=work_group_id,
                        repetition=repetition,
                        execution_order=execution_order,
                        task_completed=not candidate_success,
                    )
                )
                execution_order += 1
                observations.append(
                    _observation(
                        variant_id="ma_fixed_rag",
                        case_id=case_id,
                        work_group_id=work_group_id,
                        repetition=repetition,
                        execution_order=execution_order,
                        task_completed=candidate_success,
                    )
                )
                execution_order += 1

    comparison = compare_variants_v3(
        observations,
        baseline_variant_id="sa_shared_tools_rag",
        candidate_variant_id="ma_fixed_rag",
        metric=EvaluationMetricV3.TASK_COMPLETION,
        bootstrap_samples=200,
        random_seed=91,
    )

    assert comparison.paired_work_group_count == 2
    assert comparison.paired_conversation_count == 3
    assert comparison.paired_observation_count == 6
    assert comparison.mean_delta == 0.0
    assert comparison.candidate_wins == 1
    assert comparison.candidate_losses == 1
    assert tuple(item.conversation_count for item in comparison.work_groups) == (1, 2)


def test_work_group_bootstrap_is_deterministic_and_records_controls() -> None:
    observations = tuple(
        _observation(
            variant_id=variant_id,
            case_id=f"case_{index}",
            work_group_id=f"work_{index}",
            repetition=0,
            execution_order=index * 2 + (variant_id == "ma_fixed_rag"),
            task_completed=(index % 3 != 0 if variant_id == "ma_fixed_rag" else False),
        )
        for index in range(12)
        for variant_id in ("sa_shared_tools_rag", "ma_fixed_rag")
    )
    arguments = {
        "baseline_variant_id": "sa_shared_tools_rag",
        "candidate_variant_id": "ma_fixed_rag",
        "metric": EvaluationMetricV3.TASK_COMPLETION,
        "bootstrap_samples": 500,
        "random_seed": 1337,
    }

    first = compare_variants_v3(observations, **arguments)
    second = compare_variants_v3(tuple(reversed(observations)), **arguments)

    assert first == second
    assert first.confidence_interval is not None
    assert first.confidence_interval.method == "work_group_percentile_bootstrap"
    assert first.confidence_interval.bootstrap_samples == 500
    assert first.confidence_interval.random_seed == 1337


def test_missing_cell_is_partial_with_exact_canonical_identifier() -> None:
    protocol, split, repeat_decision, gold_hash, split_hash = _analysis_context()
    observations = _complete_observations(protocol, split, repeat_decision)
    missing = observations[-1]

    analysis = _analyze(
        protocol,
        split,
        repeat_decision,
        gold_hash,
        split_hash,
        observations[:-1],
    )

    assert analysis.completion_status == "partial"
    assert analysis.missing_observation_ids == (missing.observation_id,)
    missing_issue = next(
        item for item in analysis.issues if item.code == "missing_cells"
    )
    assert missing_issue.identifiers == (missing.observation_id,)


def test_duplicate_and_foreign_cells_are_rejected() -> None:
    protocol, split, repeat_decision, gold_hash, split_hash = _analysis_context()
    observations = _complete_observations(protocol, split, repeat_decision)

    with pytest.raises(ValueError, match="duplicate observation cell"):
        _analyze(
            protocol,
            split,
            repeat_decision,
            gold_hash,
            split_hash,
            (*observations, observations[0]),
        )

    foreign = observations[0].model_copy(update={"case_id": "held_foreign_case"})
    with pytest.raises(ValueError, match="foreign observation cell"):
        _analyze(
            protocol,
            split,
            repeat_decision,
            gold_hash,
            split_hash,
            (foreign,),
        )


def test_ambiguous_work_and_invalid_ledger_force_partial() -> None:
    protocol, split, repeat_decision, gold_hash, split_hash = _analysis_context()
    observations = list(_complete_observations(protocol, split, repeat_decision))
    original = observations[0]
    observations[0] = _observation(
        variant_id=original.variant_id,
        case_id=original.case_id,
        work_group_id="work_ambiguous",
        repetition=original.repetition,
        execution_order=original.execution_order,
        task_completed=original.task_completed,
        protocol_hash=original.protocol_sha256,
        run_id=original.run_id,
        ledger_event_id="ledger_reused",
    )
    observations[1] = observations[1].model_copy(
        update={"ledger_event_ids": ("ledger_reused",)}
    )

    analysis = _analyze(
        protocol,
        split,
        repeat_decision,
        gold_hash,
        split_hash,
        observations,
    )

    assert analysis.completion_status == "partial"
    assert {item.code for item in analysis.issues} >= {
        "ambiguous_work_group",
        "invalid_ledger",
        "missing_cells",
    }
    assert original.observation_id in analysis.missing_observation_ids
    assert observations[1].observation_id in analysis.missing_observation_ids


def test_environment_or_budget_stop_cannot_be_complete() -> None:
    protocol, split, repeat_decision, gold_hash, split_hash = _analysis_context()
    observations = _complete_observations(protocol, split, repeat_decision)
    stop = EvaluationStopV3(
        code="budget_stop",
        affected_identifiers=("budget_scope=benchmark",),
        message="benchmark reservation was stopped",
    )

    analysis = _analyze(
        protocol,
        split,
        repeat_decision,
        gold_hash,
        split_hash,
        observations,
        stop=stop,
    )

    assert analysis.completion_status == "partial"
    assert analysis.missing_observation_ids == ()
    assert any(item.code == "budget_stop" for item in analysis.issues)


def test_exact_matrix_and_hashes_are_required_for_complete() -> None:
    protocol, split, repeat_decision, gold_hash, split_hash = _analysis_context()
    observations = _complete_observations(protocol, split, repeat_decision)

    complete = _analyze(
        protocol,
        split,
        repeat_decision,
        gold_hash,
        split_hash,
        observations,
    )

    assert complete.completion_status == "complete"
    assert complete.expected_observation_count == 4 * 60 * 2
    assert complete.accepted_observation_count == complete.expected_observation_count
    assert len(complete.comparisons) == 6 * len(EvaluationMetricV3)
    assert complete.analysis_sha256

    bad_decision = repeat_decision.model_copy(update={"repeat_rule_sha256": "f" * 64})
    with pytest.raises(ValueError, match="repeat decision rule hash mismatch"):
        _analyze(
            protocol,
            split,
            bad_decision,
            gold_hash,
            split_hash,
            observations,
        )


def test_baseline_win_is_reported_as_candidate_loss() -> None:
    protocol, split, repeat_decision, gold_hash, split_hash = _analysis_context()
    observations = _complete_observations(
        protocol,
        split,
        repeat_decision,
        completion_by_variant={"ma_fixed_rag": False},
    )
    analysis = _analyze(
        protocol,
        split,
        repeat_decision,
        gold_hash,
        split_hash,
        observations,
    )
    comparison = next(
        item
        for item in analysis.comparisons
        if item.baseline_variant_id == "sa_shared_tools_rag"
        and item.candidate_variant_id == "ma_fixed_rag"
        and item.metric == EvaluationMetricV3.TASK_COMPLETION
    )

    assert comparison.mean_delta == -1.0
    assert comparison.candidate_wins == 0
    assert comparison.candidate_losses == 60
    assert ("exact" + "_plan") not in analysis.model_dump_json()


def _analysis_context() -> tuple[
    EvaluationProtocolV3,
    EvaluationSplitManifestV3,
    RepeatDecisionV3,
    str,
    str,
]:
    loaded_gold = load_evaluation_gold_v3(
        PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
        PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
        project_root=PROJECT_ROOT,
    )
    loaded_experiment = load_evaluation_experiment_v3(
        PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"
    )
    assets = EvaluationAssetBindingsV3(
        evaluator_sha256="0" * 64,
        gold_sha256=loaded_gold.gold_sha256,
        split_sha256=loaded_gold.split_sha256,
        tool_contract_sha256="1" * 64,
        prompt_bundle_sha256="2" * 64,
        corpus_sha256="3" * 64,
        index_sha256="4" * 64,
        embedding_sha256="5" * 64,
        pricing_sha256="6" * 64,
        ledger_contract_sha256="7" * 64,
        scoring_rubric_sha256="8" * 64,
    )
    protocol = build_evaluation_protocol_v3(loaded_experiment, assets=assets)
    repeat_decision = RepeatDecisionV3(
        protocol_sha256=evaluation_protocol_sha256_v3(protocol),
        repeat_rule_sha256=protocol.repeat_rule_sha256,
        cost_evidence_sha256="9" * 64,
        selected_repeats=2,
        pilot_effective_cost_usd=Decimal("1"),
        projected_benchmark_cost_usd=Decimal("20"),
        per_variant=tuple(
            VariantCostProjectionV3(
                variant_id=variant_id,
                maximum_pilot_observation_cost_usd=Decimal("0.01"),
            )
            for variant_id in PACKAGE7_VARIANT_ORDER
        ),
    )
    return (
        protocol,
        loaded_gold.split,
        repeat_decision,
        loaded_gold.gold_sha256,
        loaded_gold.split_sha256,
    )


def _complete_observations(
    protocol: EvaluationProtocolV3,
    split: EvaluationSplitManifestV3,
    repeat_decision: RepeatDecisionV3,
    *,
    completion_by_variant: dict[VariantIdV3, bool] | None = None,
) -> tuple[EvaluationObservationV3, ...]:
    completion_by_variant = completion_by_variant or {}
    protocol_hash = evaluation_protocol_sha256_v3(protocol)
    observations: list[EvaluationObservationV3] = []
    for entry in split.entries:
        if entry.split != EvaluationSplitV3.HELD_OUT:
            continue
        for variant_id in PACKAGE7_VARIANT_ORDER:
            for repetition in range(repeat_decision.selected_repeats):
                observations.append(
                    _observation(
                        variant_id=variant_id,
                        case_id=entry.conversation_id,
                        work_group_id=entry.work_group_id,
                        repetition=repetition,
                        execution_order=len(observations),
                        task_completed=completion_by_variant.get(variant_id, True),
                        protocol_hash=protocol_hash,
                        run_id="run_evaluation_v3",
                    )
                )
    return tuple(observations)


def _observation(
    *,
    variant_id: VariantIdV3,
    case_id: str,
    work_group_id: str,
    repetition: int,
    execution_order: int,
    task_completed: bool,
    protocol_hash: str = "a" * 64,
    run_id: str = "run_comparison_v3",
    ledger_event_id: str | None = None,
) -> EvaluationObservationV3:
    identity = ObservationIdentityV3(
        run_id=run_id,
        protocol_sha256=protocol_hash,
        schedule_algorithm_id="package7_pilot_interleaved_v1",
        turn_kind="measured",
        variant_id=variant_id,
        case_id=case_id,
        work_group_id=work_group_id,
        repetition=repetition,
    )
    observation_id = canonical_observation_id_v3(identity)
    return EvaluationObservationV3(
        observation_id=observation_id,
        identity=identity,
        run_id=run_id,
        protocol_sha256=protocol_hash,
        variant_id=variant_id,
        case_id=case_id,
        work_group_id=work_group_id,
        repetition=repetition,
        execution_order=execution_order,
        status=TaskStatus.SUCCESS,
        task_completed=task_completed,
        answerable=True,
        abstained=False,
        claim_support=1.0,
        citation_precision=1.0,
        citation_coverage=1.0,
        document_recall=1.0,
        authorized=True,
        valid_plan=True,
        useful_continuation=True,
        duplicate_dispatch_count=0,
        end_to_end_latency_ms=100.0,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        known_cost_usd=Decimal("0.01"),
        unresolved_reserved_cost_usd=Decimal("0"),
        ledger_event_ids=(ledger_event_id or f"ledger_event_{execution_order:06d}",),
    )


def _analyze(
    protocol: EvaluationProtocolV3,
    split: EvaluationSplitManifestV3,
    repeat_decision: RepeatDecisionV3,
    gold_hash: str,
    split_hash: str,
    observations: list[EvaluationObservationV3] | tuple[EvaluationObservationV3, ...],
    *,
    stop: EvaluationStopV3 | None = None,
):
    return analyze_evaluation_v3(
        protocol,
        split,
        repeat_decision,
        observations,
        run_id="run_evaluation_v3",
        gold_sha256=gold_hash,
        split_sha256=split_hash,
        schedule_sha256=SCHEDULE_SHA256,
        bootstrap_samples=100,
        stop=stop,
    )
