"""Receipt predicates reach the judge without labels or variant identities."""

import json

import pytest

from app.evaluation.benchmark_judging import (
    _safe_error_code,
    _serialize_model_judge_input,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_judge import ModelJudgeRequestV3
from app.v2.contracts import DialogueOutcome
from tests.test_evaluation_benchmark_judge_successor import _successor_inputs
from tests.test_evaluation_v3_judge import _answer


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
    assert wire["receipt_constraints"] == {
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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
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
            "private prompt or fabricated citation secret=example",
            "judge_output_contract_rejected",
        ),
    ],
)
def test_contract_error_codes_are_specific_without_copying_arbitrary_text(
    message, expected
):
    assert _safe_error_code(ValueError(message)) == expected
