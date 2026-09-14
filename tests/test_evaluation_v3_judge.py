"""Fail-closed semantic judging and calibration tests for Evaluation v3."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerPacketV3,
    BlindedAnswerV3,
    CitationForReviewV3,
    RubricContextV3,
    RubricFactV3,
    _judgment_collection,
)
from app.evaluation.v3_comparison import ArtifactBindingsV3
from app.evaluation.v3_gold import (
    AnswerabilityV3,
    EvaluationSplitV3,
    RequiredResponseModeV3,
)
from app.evaluation.v3_judge import (
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationFreezeV3,
    DevelopmentCalibrationCaseV3,
    ModelJudgeConfigurationV3,
    ModelJudgeRequestV3,
    build_calibration_thresholds_v3,
    build_development_calibration_case_v3,
    build_model_judge_configuration_v3,
    evaluator_configuration_sha256_v3,
    freeze_calibration_thresholds_v3,
    judge_blinded_packet_v3,
    judge_prompt_sha256_v3,
    model_judge_output_schema_sha256_v3,
    run_development_calibration_case_v3,
    score_deterministic_metrics_v3,
    validate_model_judge_output_v3,
)
from app.evaluation.v3_models import EvaluationMetricV3, GenerationBindingV3

PROMPT = "Score only the blinded answer against the supplied frozen rubric."
MODEL = GenerationBindingV3(model="gpt-5.4-mini-2026-03-17")


def test_calibrated_model_judge_is_blinded_attributed_and_complete() -> None:
    configuration, answer, packet = _context()
    seen_requests: list[ModelJudgeRequestV3] = []

    def fake_judge(request: ModelJudgeRequestV3) -> object:
        seen_requests.append(request)
        return _output(request.answer)

    case = build_development_calibration_case_v3(
        calibration_id="calibration_case_one",
        answer=answer,
    )
    reference_scores = _scores(1.0)
    record = run_development_calibration_case_v3(
        configuration,
        case,
        reference_scores=reference_scores,
        judge=fake_judge,
    )
    thresholds = build_calibration_thresholds_v3(
        minimum_development_cases=1,
        maximum_absolute_error=_scores(0.0),
    )
    freeze = freeze_calibration_thresholds_v3(
        configuration,
        (record,),
        thresholds,
    )
    judgments = judge_blinded_packet_v3(
        packet,
        configuration,
        freeze,
        judge=fake_judge,
    )

    assert len(judgments) == 1
    judgment = judgments[0]
    assert judgment.judgment_mode.value == "model_judge"
    assert judgment.reviewer_id is None
    assert judgment.model_binding == MODEL
    assert judgment.calibration_sha256 == freeze.calibration_sha256
    assert set(judgment.scores) == {
        *SEMANTIC_JUDGE_METRICS_V3,
        EvaluationMetricV3.CITATION_PRECISION,
        EvaluationMetricV3.CITATION_COVERAGE,
        EvaluationMetricV3.DOCUMENT_RECALL,
    }
    assert judgment.scores[EvaluationMetricV3.CITATION_PRECISION] == 1.0
    assert judgment.score_sources[EvaluationMetricV3.CLAIM_SUPPORT] == "model_judge"
    assert (
        judgment.score_sources[EvaluationMetricV3.CITATION_PRECISION] == "deterministic"
    )
    heldout_request = seen_requests[-1]
    encoded = heldout_request.model_dump_json()
    assert heldout_request.phase == "held_out_scoring"
    assert "variant_id" not in encoded
    assert "observation_id" not in encoded
    assert "work_group_id" not in encoded
    assert "human" not in encoded


def test_model_output_rejects_malformed_or_fabricated_evidence() -> None:
    _, answer, _ = _context()
    payload = _output(answer)
    payload["extra_claim"] = "fabricated fact"
    with pytest.raises(ValidationError, match="Extra inputs"):
        validate_model_judge_output_v3(payload, answer)

    payload = _output(answer)
    cast(list[dict[str, object]], payload["verdicts"])[0]["citation_labels"] = [
        "Nguồn không tồn tại"
    ]
    with pytest.raises(ValueError, match="fabricated citations"):
        validate_model_judge_output_v3(payload, answer)

    payload = _output(answer)
    payload["output_schema_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="schema hash mismatch"):
        validate_model_judge_output_v3(payload, answer)

    fabricated_answer = answer.model_copy(
        update={
            "citations": (
                CitationForReviewV3(
                    label="Nguồn ảo",
                    evidence="Bằng chứng không có trong rubric đóng băng.",
                ),
            )
        }
    )
    unsupported_payload = _output(fabricated_answer)
    for verdict in cast(list[dict[str, object]], unsupported_payload["verdicts"]):
        verdict["citation_labels"] = ["Nguồn ảo"]
    validated = validate_model_judge_output_v3(
        unsupported_payload,
        fabricated_answer,
    )
    deterministic = score_deterministic_metrics_v3(fabricated_answer)

    assert validated.opaque_answer_id == fabricated_answer.opaque_answer_id
    assert deterministic[EvaluationMetricV3.CITATION_PRECISION] == 0.0
    assert deterministic[EvaluationMetricV3.CITATION_COVERAGE] == 0.0
    assert deterministic[EvaluationMetricV3.DOCUMENT_RECALL] == 0.0


def test_wrong_prompt_schema_model_and_calibration_hashes_fail_closed() -> None:
    configuration, _, packet = _context()

    wrong_prompt_bindings = configuration.bindings.model_copy(
        update={"judge_prompt_sha256": "e" * 64}
    )
    with pytest.raises(ValidationError, match="judge prompt hash mismatch"):
        build_model_judge_configuration_v3(
            bindings=wrong_prompt_bindings,
            model_binding=MODEL,
            judge_prompt=PROMPT,
        )

    wrong_schema_bindings = configuration.bindings.model_copy(
        update={"judge_schema_sha256": "e" * 64}
    )
    with pytest.raises(ValidationError, match="judge output schema hash mismatch"):
        build_model_judge_configuration_v3(
            bindings=wrong_schema_bindings,
            model_binding=MODEL,
            judge_prompt=PROMPT,
        )

    with pytest.raises(ValidationError):
        GenerationBindingV3(model="gpt-5.4-mini")  # type: ignore[arg-type]

    freeze = CalibrationFreezeV3(
        protocol_sha256="f" * 64,
        configuration_sha256=configuration.configuration_sha256,
        thresholds_sha256="1" * 64,
        development_record_sha256s=("2" * 64,),
        maximum_observed_errors=_scores(0.0),
        calibration_sha256=canonical_sha256(
            {
                "schema_version": "3.0",
                "protocol_sha256": "f" * 64,
                "configuration_sha256": configuration.configuration_sha256,
                "thresholds_sha256": "1" * 64,
                "development_record_sha256s": ("2" * 64,),
                "maximum_observed_errors": _scores(0.0),
            }
        ),
    )
    with pytest.raises(ValueError, match="different benchmark protocol"):
        judge_blinded_packet_v3(
            packet,
            configuration,
            freeze,
            judge=lambda request: _output(request.answer),
        )


def test_calibration_contract_cannot_accept_heldout_outputs() -> None:
    _, answer, _ = _context()
    case = build_development_calibration_case_v3(
        calibration_id="calibration_development_only",
        answer=answer,
    )
    payload = case.model_dump(mode="json")
    payload["split"] = "held_out"
    with pytest.raises(ValidationError):
        DevelopmentCalibrationCaseV3.model_validate(payload)

    payload = case.model_dump(mode="json")
    payload["heldout_answer_output"] = "leak"
    with pytest.raises(ValidationError, match="Extra inputs"):
        DevelopmentCalibrationCaseV3.model_validate(payload)


def test_judgment_collection_rejects_incomplete_blind_packet() -> None:
    configuration, answer, packet = _context(answer_count=2)
    case = build_development_calibration_case_v3(
        calibration_id="calibration_for_completeness",
        answer=answer,
    )
    record = run_development_calibration_case_v3(
        configuration,
        case,
        reference_scores=_scores(1.0),
        judge=lambda request: _output(request.answer),
    )
    thresholds = build_calibration_thresholds_v3(
        minimum_development_cases=1,
        maximum_absolute_error=_scores(0.0),
    )
    freeze = freeze_calibration_thresholds_v3(
        configuration,
        (record,),
        thresholds,
    )
    one_judgment = judge_blinded_packet_v3(
        packet,
        configuration,
        freeze,
        judge=lambda request: _output(request.answer),
    )[0]

    with pytest.raises(ValueError, match="incomplete"):
        _judgment_collection(packet, (one_judgment,))


def test_artifact_bindings_expose_every_required_provenance_hash() -> None:
    bindings = _bindings()

    expected = {
        "protocol_sha256",
        "gold_sha256",
        "split_sha256",
        "schedule_sha256",
        "repeat_decision_sha256",
        "evaluator_configuration_sha256",
        "judge_prompt_sha256",
        "judge_schema_sha256",
        "tool_contract_sha256",
        "corpus_sha256",
        "index_sha256",
        "embedding_model_dimensions_sha256",
        "pricing_manifest_sha256",
        "usage_ledger_sha256",
        "rubric_sha256",
    }
    assert set(bindings.model_dump()) == expected
    assert all(len(value) == 64 for value in bindings.model_dump().values())


def _context(
    *, answer_count: int = 1
) -> tuple[
    ModelJudgeConfigurationV3,
    BlindedAnswerV3,
    BlindedAnswerPacketV3,
]:
    bindings = _bindings()
    configuration = build_model_judge_configuration_v3(
        bindings=bindings,
        model_binding=MODEL,
        judge_prompt=PROMPT,
    )
    answer = _answer("answer_000000000000000000000001")
    answers = tuple(
        _answer(f"answer_{index + 1:024x}") for index in range(answer_count)
    )
    return configuration, answer, _packet(bindings, answers)


def _bindings() -> ArtifactBindingsV3:
    judge_prompt_sha256 = judge_prompt_sha256_v3(PROMPT)
    judge_schema_sha256 = model_judge_output_schema_sha256_v3()
    rubric_sha256 = "d" * 64
    evaluator_sha256 = evaluator_configuration_sha256_v3(
        model_binding=MODEL,
        judge_prompt_sha256=judge_prompt_sha256,
        judge_schema_sha256=judge_schema_sha256,
        rubric_sha256=rubric_sha256,
    )
    return ArtifactBindingsV3(
        protocol_sha256="0" * 64,
        gold_sha256="1" * 64,
        split_sha256="2" * 64,
        schedule_sha256="3" * 64,
        repeat_decision_sha256="4" * 64,
        evaluator_configuration_sha256=evaluator_sha256,
        judge_prompt_sha256=judge_prompt_sha256,
        judge_schema_sha256=judge_schema_sha256,
        tool_contract_sha256="5" * 64,
        corpus_sha256="6" * 64,
        index_sha256="7" * 64,
        embedding_model_dimensions_sha256="8" * 64,
        pricing_manifest_sha256="9" * 64,
        usage_ledger_sha256="a" * 64,
        rubric_sha256=rubric_sha256,
    )


def _answer(opaque_answer_id: str) -> BlindedAnswerV3:
    evidence = "Bằng chứng đóng băng cho sự kiện bắt buộc."
    return BlindedAnswerV3(
        opaque_answer_id=opaque_answer_id,
        prompt=("Câu hỏi đã được che danh tính.",),
        answer="Câu trả lời có dẫn nguồn đóng băng.",
        citations=(CitationForReviewV3(label="Nguồn 1", evidence=evidence),),
        rubric_context=RubricContextV3(
            answerability=AnswerabilityV3.ANSWERABLE,
            required_response_mode=RequiredResponseModeV3.DIRECT_ANSWER,
            required_facts=(
                RubricFactV3(
                    claim="Sự kiện bắt buộc",
                    expected_value=True,
                    evidence=evidence,
                ),
            ),
            forbidden_claims=(),
            expected_action_outcome="complete",
        ),
    )


def _packet(
    bindings: ArtifactBindingsV3,
    answers: tuple[BlindedAnswerV3, ...],
) -> BlindedAnswerPacketV3:
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.HELD_OUT,
        "bindings": bindings.model_dump(mode="json"),
        "randomization_seed": 42,
        "answer_count": len(answers),
        "answers": [item.model_dump(mode="json") for item in answers],
    }
    return BlindedAnswerPacketV3(
        bindings=bindings,
        randomization_seed=42,
        answer_count=len(answers),
        answers=answers,
        packet_sha256=canonical_sha256(payload),
    )


def _scores(value: float) -> dict[EvaluationMetricV3, float]:
    return {metric: value for metric in SEMANTIC_JUDGE_METRICS_V3}


def _output(answer: BlindedAnswerV3) -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "output_schema_sha256": model_judge_output_schema_sha256_v3(),
        "opaque_answer_id": answer.opaque_answer_id,
        "verdicts": [
            {
                "metric": metric,
                "score": 1.0,
                "rubric_fact_indices": [0],
                "citation_labels": ["Nguồn 1"],
            }
            for metric in SEMANTIC_JUDGE_METRICS_V3
        ],
    }
