"""Receipt predicates reach the judge without labels or variant identities."""

import json

import pytest

from app.evaluation.benchmark_judging import (
    _safe_error_code,
    _serialize_model_judge_input,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    CitationForReviewV3,
    ClaimEvidenceV3,
    RubricFactV3,
)
from app.evaluation.v3_judge import (
    ModelJudgeRequestV3,
    score_runtime_metrics_v3,
    validate_model_judge_output_v3,
)
from app.evaluation.v3_models import EvaluationMetricV3
from app.v2.contracts import DialogueOutcome
from tests.test_evaluation_benchmark_judge_successor import _successor_inputs
from tests.test_evaluation_v3_judge import _answer


@pytest.mark.parametrize(
    "kind",
    [
        "catalog",
        "insufficient",
        "grounding",
        "required_fact",
        "assertion",
        "claim",
        "citation",
    ],
)
def test_canonical_empty_response_support_is_separate_from_helpfulness(kind):
    row, _, _, _ = _successor_inputs()
    configuration, case, _, _, _ = row
    original = case.answer
    replies = {
        "catalog": (
            "Mình chưa tìm thấy đủ ứng viên đã xác minh; "
            "bạn hãy nêu rõ hơn tên sách hoặc tác giả."
        ),
        "insufficient": (
            "Mình chưa có đủ bằng chứng đã xác minh để trả lời yêu cầu này."
        ),
        "grounding": "Không có đủ bằng chứng đã kiểm tra để trả lời.",
    }
    answer = original.model_copy(
        update={
            "answer": replies.get(kind, replies["catalog"]),
            "claims": (),
            "citations": (),
            "rubric_context": original.rubric_context.model_copy(
                update={"required_facts": ()}
            ),
            "runtime_evidence": original.runtime_evidence.model_copy(
                update={"outcome": DialogueOutcome.NEEDS_CLARIFICATION}
            ),
        }
    )
    if kind == "required_fact":
        answer = answer.model_copy(
            update={
                "rubric_context": answer.rubric_context.model_copy(
                    update={
                        "required_facts": _answer(
                            answer.opaque_answer_id
                        ).rubric_context.required_facts
                    }
                )
            }
        )
    elif kind == "assertion":
        answer = answer.model_copy(
            update={"answer": answer.answer + " Sách này giá 99999 VND."}
        )
    elif kind == "claim":
        answer = answer.model_copy(
            update={
                "claims": (
                    ClaimEvidenceV3(
                        text="Sách này đoạt một giải thưởng.", citation_labels=()
                    ),
                )
            }
        )
    elif kind == "citation":
        answer = answer.model_copy(update={"citations": (original.citations[0],)})
    before = canonical_sha256(answer)
    scores = score_runtime_metrics_v3(answer)
    scores[EvaluationMetricV3.CLAIM_SUPPORT] = 0.0
    scores[EvaluationMetricV3.TASK_COMPLETION] = 0.0
    payload = {
        "schema_version": "3.0",
        "claim_coverage_contract": "receipt_claim_citation_coverage_v1",
        "output_schema_sha256": configuration.bindings.judge_schema_sha256,
        "opaque_answer_id": answer.opaque_answer_id,
        "verdicts": [
            {
                "metric": metric.value,
                "score": score,
                "rubric_fact_indices": [],
                "citation_labels": [],
            }
            for metric, score in scores.items()
        ],
        "claim_verdicts": [
            {
                "claim_index": 0,
                "supported": False,
                "citation_labels": [],
                "rubric_fact_indices": [],
            }
        ]
        if answer.claims
        else [],
    }
    vacuous = kind in replies
    if vacuous:
        with pytest.raises(ValueError, match="canonical empty response"):
            validate_model_judge_output_v3(payload, answer)
        scores[EvaluationMetricV3.CLAIM_SUPPORT] = 1.0
        scores[EvaluationMetricV3.TASK_COMPLETION] = float(
            all(
                value == 1.0
                for metric, value in scores.items()
                if metric is not EvaluationMetricV3.TASK_COMPLETION
            )
        )
        payload["verdicts"] = [
            {
                "metric": metric.value,
                "score": score,
                "rubric_fact_indices": [],
                "citation_labels": [],
            }
            for metric, score in scores.items()
        ]
    validate_model_judge_output_v3(payload, answer)
    request = ModelJudgeRequestV3(
        phase="development_calibration",
        judge_prompt=configuration.judge_prompt,
        output_schema_sha256=configuration.bindings.judge_schema_sha256,
        configuration_sha256=configuration.configuration_sha256,
        answer=answer,
    )
    wire = json.loads(_serialize_model_judge_input(request))
    assert wire["receipt_constraints"]["claim_support_must_be_one"] is vacuous
    assert canonical_sha256(wire["answer"]) == canonical_sha256(answer) == before
    assert "reference_scores" not in wire


@pytest.mark.parametrize("has_claims", [False, True])
def test_successor_wire_supplies_receipt_constraints_without_replacing_answer(
    has_claims,
):
    row, _, _, _ = _successor_inputs()
    configuration, case, _, _, _ = row
    answer = case.answer
    runtime = answer.runtime_evidence.model_copy(
        update={
            "outcome": DialogueOutcome.NEEDS_CLARIFICATION,
            "successful_capabilities": ("merchant.price.update",),
        }
    )
    answer = answer.model_copy(
        update={
            "runtime_evidence": runtime,
            "claims": answer.claims if has_claims else (),
            "rubric_context": answer.rubric_context.model_copy(
                update={
                    "required_facts": _answer(
                        answer.opaque_answer_id
                    ).rubric_context.required_facts
                }
            ),
        }
    )
    original_hash = canonical_sha256(answer)
    request = ModelJudgeRequestV3(
        phase="development_calibration",
        judge_prompt=configuration.judge_prompt,
        output_schema_sha256=configuration.bindings.judge_schema_sha256,
        configuration_sha256=configuration.configuration_sha256,
        answer=answer,
    )
    wire = json.loads(_serialize_model_judge_input(request))
    assert {
        key: wire["receipt_constraints"][key]
        for key in ("runtime_metric_scores", "claim_support_must_be_zero")
    } == {
        "runtime_metric_scores": {
            "answerability_abstention": 0.0,
            "authorization": 0.0,
            "valid_plan": 0.0,
            "useful_continuation": 0.0,
        },
        "claim_support_must_be_zero": not has_claims,
    }
    assert canonical_sha256(wire["answer"]) == original_hash
    assert canonical_sha256(answer) == original_hash
    assert "variant_id" not in _serialize_model_judge_input(request)
    assert "reference_scores" not in _serialize_model_judge_input(request)


@pytest.mark.parametrize("claimed_price", [191000, 99999])
def test_claim_binding_hints_keep_demo_price_separate_from_catalog_facts(
    claimed_price,
):
    row, _, _, _ = _successor_inputs()
    configuration, case, _, _, _ = row
    answer = case.answer.model_copy(
        update={
            "citations": (
                CitationForReviewV3(
                    label="[C1]",
                    source_id="catalog_product_80",
                    source_version_id="catalog_version_test",
                    evidence="snapshot_price_vnd: 191000 VND",
                ),
                CitationForReviewV3(
                    label="[C2]",
                    source_id="sandbox_offer_80",
                    source_version_id="catalog_version_test",
                    evidence="demo_price_vnd: 191000 VND",
                ),
            ),
            "claims": (
                ClaimEvidenceV3(
                    text=f"Giá snapshot: {claimed_price} VND",
                    citation_labels=("[C1]",),
                ),
                ClaimEvidenceV3(text="Giá demo: 191000 VND", citation_labels=("[C2]",)),
            ),
            "rubric_context": case.answer.rubric_context.model_copy(
                update={
                    "required_facts": (
                        RubricFactV3(
                            claim="Giá snapshot là 191000 VND",
                            expected_value=191000,
                            evidence="Frozen catalog price field",
                            match_mode="numeric_exact",
                            support_kind="catalog_pointer",
                            support_record_id="product_80",
                            support_json_pointer="/price_vnd",
                        ),
                    )
                }
            ),
        }
    )
    before = canonical_sha256(answer)
    request = ModelJudgeRequestV3(
        phase="development_calibration",
        judge_prompt=configuration.judge_prompt,
        output_schema_sha256=configuration.bindings.judge_schema_sha256,
        configuration_sha256=configuration.configuration_sha256,
        answer=answer,
    )
    wire = json.loads(_serialize_model_judge_input(request))
    assert wire["receipt_constraints"]["claim_fact_bindings"] == [
        {
            "claim_index": 0,
            "citations": [{"label": "[C1]", "eligible_required_fact_indices": [0]}],
        },
        {
            "claim_index": 1,
            "citations": [{"label": "[C2]", "eligible_required_fact_indices": []}],
        },
    ]
    # Eligibility describes source bindings, including for an incorrect claim;
    # it never supplies a truth label or rewrites the receipt-bound answer.
    assert canonical_sha256(wire["answer"]) == canonical_sha256(answer) == before
    assert "supported" not in wire["receipt_constraints"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "canonical empty response requires vacuous claim support",
            "judge_claim_vacuity_mismatch",
        ),
        (
            "model verdict differs from receipt-bound runtime predicates",
            "judge_runtime_predicate_mismatch",
        ),
        (
            "task completion differs from component verdicts",
            "judge_task_completion_mismatch",
        ),
        (
            "positive claim support lacks per-claim required facts",
            "judge_required_fact_support_missing",
        ),
        (
            "model output schema hash mismatch",
            "judge_output_schema_hash_mismatch",
        ),
        (
            "supported claim lacks bound fact evidence",
            "judge_claim_fact_binding_mismatch",
        ),
        (
            "supported claim violates exact fact matching",
            "judge_claim_exact_fact_mismatch",
        ),
        (
            "claim verdict borrows another claim's cited evidence",
            "judge_claim_citation_ownership_mismatch",
        ),
        (
            "claim verdict references an unknown citation",
            "judge_claim_citation_unknown",
        ),
        (
            "claim verdict references an unknown rubric fact",
            "judge_fact_unknown",
        ),
        (
            "positive claim support lacks bound fact evidence",
            "judge_answer_fact_binding_mismatch",
        ),
        (
            "private prompt or fabricated citation secret=example",
            "judge_output_contract_rejected",
        ),
    ],
)
def test_contract_error_codes_are_specific_without_copying_arbitrary_text(
    message, expected
):
    assert _safe_error_code(ValueError(message)) == expected
