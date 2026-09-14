"""Canonical reporting, blinding, and judgment attribution tests for v3."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts import TaskStatus
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    AnswerEvidenceInputV3,
    CitationForReviewV3,
    build_blinded_answer_packet_v3,
    build_evaluation_report_v3,
    build_judgment_record_v3,
    validate_evaluation_artifacts_v3,
    write_evaluation_artifacts_v3,
)
from app.evaluation.v3_comparison import EvaluationAnalysisV3, analyze_evaluation_v3
from app.evaluation.v3_gold import load_evaluation_gold_v3
from app.evaluation.v3_models import (
    PACKAGE7_VARIANT_ORDER,
    EvaluationAssetBindingsV3,
    EvaluationMetricV3,
    EvaluationObservationV3,
    GenerationBindingV3,
    JudgmentModeV3,
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


def test_report_preserves_baseline_win_without_filtering() -> None:
    protocol, loaded_gold, repeat_decision, analysis, _ = _partial_analysis()
    report = build_evaluation_report_v3(analysis)
    comparison = next(
        item
        for item in report.comparisons
        if item.baseline_variant_id == "sa_shared_tools_rag"
        and item.candidate_variant_id == "ma_fixed_rag"
        and item.metric == EvaluationMetricV3.TASK_COMPLETION
    )

    assert report.completion_status == "partial"
    assert comparison.mean_delta == -1.0
    assert comparison.candidate_losses == 1
    assert report.bindings.gold_sha256 == loaded_gold.gold_sha256
    assert report.bindings.protocol_sha256 == evaluation_protocol_sha256_v3(protocol)


def test_blind_packet_is_deterministic_and_has_separate_identity_key() -> None:
    protocol, loaded_gold, _, analysis, observations = _partial_analysis()
    answers = _answer_inputs(observations)

    first = build_blinded_answer_packet_v3(
        protocol,
        loaded_gold.gold,
        analysis,
        answers,
        random_seed=2207,
    )
    second = build_blinded_answer_packet_v3(
        protocol,
        loaded_gold.gold,
        analysis,
        tuple(reversed(answers)),
        random_seed=2207,
    )

    assert first == second
    assert first.packet.packet_sha256
    assert first.unblinding_key.key_sha256
    encoded = first.packet.model_dump_json()
    for answer in answers:
        assert answer.variant_id not in encoded
        assert answer.observation_id not in encoded
        assert answer.conversation_id not in encoded
        assert answer.work_group_id not in encoded
    assert protocol.variants[0].generation_binding.model not in encoded
    assert all(item.prompt for item in first.packet.answers)
    assert all(item.rubric_context.required_facts for item in first.packet.answers)
    assert {item.observation_id for item in first.unblinding_key.entries} == {
        item.observation_id for item in answers
    }


def test_blind_packet_rejects_answer_text_that_leaks_execution_identity() -> None:
    protocol, loaded_gold, _, analysis, observations = _partial_analysis()
    answers = list(_answer_inputs(observations))
    answers[0] = answers[0].model_copy(update={"answer": answers[0].observation_id})

    with pytest.raises(ValueError, match="identifying execution metadata"):
        build_blinded_answer_packet_v3(
            protocol,
            loaded_gold.gold,
            analysis,
            answers,
            random_seed=2207,
        )


def test_model_and_human_judgments_require_truthful_attribution() -> None:
    protocol, loaded_gold, _, analysis, observations = _partial_analysis()
    blinded = build_blinded_answer_packet_v3(
        protocol,
        loaded_gold.gold,
        analysis,
        _answer_inputs(observations),
        random_seed=42,
    )
    opaque_id = blinded.packet.answers[0].opaque_answer_id
    human = build_judgment_record_v3(
        bindings=analysis.bindings,
        blinded_packet_sha256=blinded.packet.packet_sha256,
        opaque_answer_id=opaque_id,
        judgment_mode=JudgmentModeV3.HUMAN_REVIEW,
        reviewer_id="reviewer_alpha",
        scores={EvaluationMetricV3.CLAIM_SUPPORT: 1.0},
    )
    model = build_judgment_record_v3(
        bindings=analysis.bindings,
        blinded_packet_sha256=blinded.packet.packet_sha256,
        opaque_answer_id=opaque_id,
        judgment_mode=JudgmentModeV3.MODEL_JUDGE,
        model_binding=GenerationBindingV3(
            model="gpt-5.4-mini-2026-03-17",
        ),
        scores={EvaluationMetricV3.CLAIM_SUPPORT: 0.5},
    )

    assert human.judgment_mode == JudgmentModeV3.HUMAN_REVIEW
    assert human.reviewer_id == "reviewer_alpha"
    assert human.model_binding is None
    assert model.judgment_mode == JudgmentModeV3.MODEL_JUDGE
    assert model.reviewer_id is None
    assert model.model_binding is not None

    with pytest.raises(ValueError, match="model_judge requires a model binding"):
        build_judgment_record_v3(
            bindings=analysis.bindings,
            blinded_packet_sha256=blinded.packet.packet_sha256,
            opaque_answer_id=opaque_id,
            judgment_mode=JudgmentModeV3.MODEL_JUDGE,
            reviewer_id="reviewer_wrong",
            scores={EvaluationMetricV3.CLAIM_SUPPORT: 0.5},
        )


def test_written_artifacts_have_stable_canonical_hashes_and_validate(
    tmp_path: Path,
) -> None:
    protocol, loaded_gold, _, analysis, observations = _partial_analysis()
    report = build_evaluation_report_v3(analysis)
    blinded = build_blinded_answer_packet_v3(
        protocol,
        loaded_gold.gold,
        analysis,
        _answer_inputs(observations),
        random_seed=7,
    )
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first = write_evaluation_artifacts_v3(
        first_dir,
        analysis=analysis,
        report=report,
        blinded=blinded,
    )
    second = write_evaluation_artifacts_v3(
        second_dir,
        analysis=analysis,
        report=report,
        blinded=blinded,
    )

    assert first == second
    assert (
        validate_evaluation_artifacts_v3(
            first_dir,
            expected_bindings=analysis.bindings,
        )
        == first
    )
    assert validate_evaluation_artifacts_v3(second_dir) == second
    with pytest.raises(ValueError, match="provenance differs"):
        validate_evaluation_artifacts_v3(
            first_dir,
            expected_bindings=analysis.bindings.model_copy(
                update={"usage_ledger_sha256": "f" * 64}
            ),
        )
    assert {path.name: path.read_bytes() for path in first_dir.iterdir()} == {
        path.name: path.read_bytes() for path in second_dir.iterdir()
    }


def test_complete_bundle_requires_judgments_but_partial_pre_review_is_allowed(
    tmp_path: Path,
) -> None:
    protocol, loaded_gold, _, partial, observations = _partial_analysis()
    blinded = build_blinded_answer_packet_v3(
        protocol,
        loaded_gold.gold,
        partial,
        _answer_inputs(observations),
        random_seed=17,
    )

    partial_directory = tmp_path / "partial-pre-review"
    partial_manifest = write_evaluation_artifacts_v3(
        partial_directory,
        analysis=partial,
        report=build_evaluation_report_v3(partial),
        blinded=blinded,
    )
    assert partial_manifest.completion_status == "partial"
    assert validate_evaluation_artifacts_v3(partial_directory) == partial_manifest

    complete_payload = partial.model_dump(mode="json", exclude={"analysis_sha256"})
    complete_payload.update(
        {
            "completion_status": "complete",
            "expected_observation_count": partial.accepted_observation_count,
            "missing_observation_ids": (),
            "issues": (),
        }
    )
    complete = EvaluationAnalysisV3.model_validate(
        {
            **complete_payload,
            "analysis_sha256": canonical_sha256(complete_payload),
        }
    )
    with pytest.raises(ValueError, match="complete evaluation artifacts require"):
        write_evaluation_artifacts_v3(
            tmp_path / "complete-without-judgments",
            analysis=complete,
            report=build_evaluation_report_v3(complete),
            blinded=blinded,
        )


def _partial_analysis():
    loaded_gold = load_evaluation_gold_v3(
        PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
        PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
        project_root=PROJECT_ROOT,
    )
    experiment = load_evaluation_experiment_v3(
        PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"
    )
    protocol = build_evaluation_protocol_v3(
        experiment,
        assets=EvaluationAssetBindingsV3(
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
        ),
    )
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
    case = next(
        item
        for item in loaded_gold.gold.conversations
        if item.split.value == "held_out"
    )
    protocol_hash = evaluation_protocol_sha256_v3(protocol)
    observations = tuple(
        _observation(
            protocol_hash=protocol_hash,
            variant_id=variant_id,
            case_id=case.conversation_id,
            work_group_id=case.work_group_id,
            execution_order=index,
            task_completed=variant_id == "sa_shared_tools_rag",
        )
        for index, variant_id in enumerate(("sa_shared_tools_rag", "ma_fixed_rag"))
    )
    analysis = analyze_evaluation_v3(
        protocol,
        loaded_gold.split,
        repeat_decision,
        observations,
        run_id="run_artifacts_v3",
        gold_sha256=loaded_gold.gold_sha256,
        split_sha256=loaded_gold.split_sha256,
        schedule_sha256="a" * 64,
        bootstrap_samples=100,
    )
    return protocol, loaded_gold, repeat_decision, analysis, observations


def _observation(
    *,
    protocol_hash: str,
    variant_id: VariantIdV3,
    case_id: str,
    work_group_id: str,
    execution_order: int,
    task_completed: bool,
) -> EvaluationObservationV3:
    identity = ObservationIdentityV3(
        run_id="run_artifacts_v3",
        protocol_sha256=protocol_hash,
        schedule_algorithm_id="package7_pilot_interleaved_v1",
        turn_kind="measured",
        variant_id=variant_id,
        case_id=case_id,
        work_group_id=work_group_id,
        repetition=0,
    )
    return EvaluationObservationV3(
        observation_id=canonical_observation_id_v3(identity),
        identity=identity,
        run_id=identity.run_id,
        protocol_sha256=protocol_hash,
        variant_id=variant_id,
        case_id=case_id,
        work_group_id=work_group_id,
        repetition=0,
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
        end_to_end_latency_ms=90,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        known_cost_usd=Decimal("0.01"),
        unresolved_reserved_cost_usd=Decimal("0"),
        ledger_event_ids=(f"ledger_artifact_{execution_order}",),
    )


def _answer_inputs(
    observations: tuple[EvaluationObservationV3, ...],
) -> tuple[AnswerEvidenceInputV3, ...]:
    return tuple(
        AnswerEvidenceInputV3(
            observation_id=item.observation_id,
            variant_id=item.variant_id,
            conversation_id=item.case_id,
            work_group_id=item.work_group_id,
            repetition=item.repetition,
            answer="Câu trả lời đã được chấm theo bằng chứng đóng băng.",
            citations=(
                CitationForReviewV3(
                    label="Nguồn 1",
                    evidence="Đoạn bằng chứng liên quan đến câu trả lời.",
                ),
            ),
        )
        for item in observations
    )
