"""Independent counterexamples for successor evaluator matching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerV3,
    CitationForReviewV3,
    ClaimCitationCoverageV3,
    ClaimCitationVerdictV3,
    ClaimEvidenceV3,
    RubricContextV3,
    RubricFactV3,
    RuntimeRubricEvidenceV3,
    build_rubric_context_v3,
)
from app.evaluation.v3_gold import GoldConversationV3
from app.evaluation.v3_judge import (
    SEMANTIC_JUDGE_METRICS_V3,
    claim_citation_coverage_from_output_v3,
    model_judge_output_schema_sha256_v3,
    score_deterministic_metrics_v3,
    validate_model_judge_output_v3,
)
from app.evaluation.v3_matching import citation_supports_fact_v3, exact_value_in_text_v3
from app.evaluation.v3_models import EvaluationMetricV3


@pytest.mark.parametrize("text", ["15", "15.0", "15.0/5", "5/15", "50", "x5", "5.1"])
def test_numeric_exact_rejects_substrings_and_denominators(text: str) -> None:
    assert not exact_value_in_text_v3(5, text, numeric=True)


@pytest.mark.parametrize("text", ["rating: 5", "rating: 5.0 rating_5", "5,00 điểm"])
def test_numeric_exact_accepts_equal_decimal_values(text: str) -> None:
    assert exact_value_in_text_v3(5, text, numeric=True)


def test_catalog_representation_matches_subject_and_field() -> None:
    fact = _fact()
    citation = _citation("snapshot_rating: 5.0 rating_5")
    assert citation_supports_fact_v3(citation, fact)
    assert not citation_supports_fact_v3(_citation("snapshot_price_vnd: 5 VND"), fact)
    assert not citation_supports_fact_v3(_citation("snapshot_rating: 15.0/5"), fact)
    assert not citation_supports_fact_v3(
        citation.model_copy(update={"source_id": "catalog_product_158"}), fact
    )
    assert not citation_supports_fact_v3(
        citation.model_copy(update={"source_version_id": None}), fact
    )


def test_rubric_preserves_match_modes_and_legacy_serialization() -> None:
    raw = json.loads(Path("evaluation/v3/gold.v3.json").read_text(encoding="utf-8"))[
        "conversations"
    ][0]
    case = GoldConversationV3.model_validate(raw)
    rubric = build_rubric_context_v3(case)
    assert tuple(fact.match_mode for fact in rubric.required_facts) == tuple(
        fact.match_mode for fact in case.required_fact_blueprints
    )
    legacy = build_rubric_context_v3(case, legacy=True)
    legacy_payload = legacy.model_dump(mode="json")
    assert "evaluator_contract" not in legacy_payload
    assert all("match_mode" not in fact for fact in legacy_payload["required_facts"])
    assert canonical_sha256(legacy_payload) == canonical_sha256(
        RubricContextV3.model_validate(legacy_payload)
    )


def test_document_recall_is_unmeasured_and_coverage_counts_claims() -> None:
    answer = _answer()
    scores = score_deterministic_metrics_v3(answer, claim_coverage=_coverage(answer))
    assert scores[EvaluationMetricV3.CITATION_PRECISION] == 1.0
    assert scores[EvaluationMetricV3.CITATION_COVERAGE] == 1.0
    assert scores[EvaluationMetricV3.DOCUMENT_RECALL] is None
    missing = answer.model_copy(
        update={
            "citations": (),
            "claims": (ClaimEvidenceV3(text="Rating 5", citation_labels=()),),
        }
    )
    assert (
        score_deterministic_metrics_v3(
            missing, claim_coverage=_coverage(missing, supported=(False,))
        )[EvaluationMetricV3.CITATION_COVERAGE]
        == 0.0
    )
    with pytest.raises(ValueError, match="per-claim verdicts"):
        score_deterministic_metrics_v3(answer)


def test_claim_coverage_is_not_missing_required_fact_recall() -> None:
    answer = _answer()
    answer = answer.model_copy(
        update={
            "rubric_context": answer.rubric_context.model_copy(
                update={
                    "required_facts": (
                        _fact(),
                        _fact().model_copy(
                            update={
                                "claim": "title",
                                "expected_value": "Sapiens",
                                "match_mode": "normalized_exact",
                                "support_json_pointer": "/name",
                            }
                        ),
                    )
                }
            )
        }
    )
    # This answer misses one required fact, but its only claim is supported.
    scores = score_deterministic_metrics_v3(answer, claim_coverage=_coverage(answer))
    assert scores[EvaluationMetricV3.CITATION_COVERAGE] == 1.0


def test_extra_unsupported_award_decreases_claim_coverage() -> None:
    answer = _answer().model_copy(
        update={
            "claims": (
                ClaimEvidenceV3(text="Rating 5", citation_labels=("[C1]",)),
                ClaimEvidenceV3(text="Giành Nobel năm 2024", citation_labels=("[C1]",)),
            )
        }
    )
    coverage = _coverage(answer, supported=(True, False))
    assert coverage.claim_count == 2
    assert coverage.supported_claim_count == 1
    assert (
        score_deterministic_metrics_v3(answer, claim_coverage=coverage)[
            EvaluationMetricV3.CITATION_COVERAGE
        ]
        == 0.5
    )
    with pytest.raises(ValueError, match="conflicts with per-claim"):
        validate_model_judge_output_v3(_output(answer, supported=(True, False)), answer)


def test_own_citation_borrowing_cannot_inflate_coverage() -> None:
    answer = _answer().model_copy(
        update={
            "claims": (
                ClaimEvidenceV3(text="Rating 5", citation_labels=()),
                ClaimEvidenceV3(text="Rating 5", citation_labels=("[C1]",)),
            )
        }
    )
    raw = _output(answer)
    raw["claim_verdicts"] = [
        {
            "claim_index": index,
            "supported": True,
            "citation_labels": ["[C1]"],
            "rubric_fact_indices": [0],
        }
        for index in range(2)
    ]
    with pytest.raises(ValueError, match="borrows another claim"):
        validate_model_judge_output_v3(raw, answer)


@pytest.mark.parametrize("indices", [(0,), (1, 0), (0, 0), (0, 2)])
def test_claim_coverage_requires_every_ordered_receipt_claim(indices: tuple[int, ...]):
    with pytest.raises(ValueError, match="every claim in order"):
        ClaimCitationCoverageV3(
            answer_sha256="a" * 64,
            claim_count=2,
            supported_claim_count=0,
            verdicts=tuple(
                ClaimCitationVerdictV3(claim_index=index, supported=False)
                for index in indices
            ),
            coverage=0.0,
        )


def test_zero_claim_coverage_is_unmeasured_and_uncited_claim_is_zero() -> None:
    answer = _answer().model_copy(update={"claims": ()})
    coverage = _coverage(answer)
    assert coverage.claim_count == 0
    assert coverage.coverage is None
    assert (
        score_deterministic_metrics_v3(answer, claim_coverage=coverage)[
            EvaluationMetricV3.CITATION_COVERAGE
        ]
        is None
    )
    answer = _answer().model_copy(
        update={"claims": (ClaimEvidenceV3(text="Rating 5", citation_labels=()),)}
    )
    assert _coverage(answer, supported=(False,)).coverage == 0.0
    with pytest.raises(ValueError, match="own cited evidence"):
        _coverage(answer)


def test_claim_coverage_rejects_forged_hash_ratio_and_denominator() -> None:
    answer = _answer()
    coverage = _coverage(answer)
    for tampered, error in [
        (coverage.model_copy(update={"answer_sha256": "f" * 64}), "receipt-bound"),
        (coverage.model_copy(update={"coverage": 0.5}), "per-claim verdicts"),
        (coverage.model_copy(update={"claim_count": 2}), "every claim in order"),
    ]:
        with pytest.raises(ValueError, match=error):
            score_deterministic_metrics_v3(answer, claim_coverage=tampered)


def test_legacy_model_output_schema_hash_is_unchanged() -> None:
    assert model_judge_output_schema_sha256_v3() == (
        "da22dbd1a8ade71f6f19b5f3288530e09d9d8baadf2013c210449b859049cd2a"
    )
    assert model_judge_output_schema_sha256_v3(successor=True) != (
        model_judge_output_schema_sha256_v3()
    )


def test_positive_numeric_verdict_cannot_borrow_unrelated_claim_citation() -> None:
    answer = _answer().model_copy(
        update={
            "claims": (
                ClaimEvidenceV3(text="Rating 15.0/5", citation_labels=("[C1]",)),
            )
        }
    )
    with pytest.raises(ValueError, match="exact fact"):
        validate_model_judge_output_v3(_output(answer), answer)
    answer = _answer().model_copy(
        update={"claims": (ClaimEvidenceV3(text="Rating 5", citation_labels=()),)}
    )
    with pytest.raises(ValueError, match="own cited evidence"):
        validate_model_judge_output_v3(_output(answer), answer)


def test_rating_numerator_matches_without_borrowing_scale_denominator() -> None:
    assert exact_value_in_text_v3(5, "Điểm đánh giá 5/5", numeric=True)
    assert exact_value_in_text_v3(4.8, "Điểm đánh giá 4,8/5", numeric=True)
    assert not exact_value_in_text_v3(5, "Điểm đánh giá 4.8/5", numeric=True)
    assert not exact_value_in_text_v3(5, "Điểm đánh giá 15.0/5", numeric=True)
    answer = _answer().model_copy(
        update={
            "claims": (ClaimEvidenceV3(text="Rating 5/5", citation_labels=("[C1]",)),),
        }
    )
    validate_model_judge_output_v3(_output(answer), answer)


def test_semantic_paraphrase_is_not_required_to_contain_english_gold() -> None:
    fact = RubricFactV3(
        claim="work subject",
        expected_value="family life",
        evidence="The novel concerns family life.",
        match_mode="fact_semantics",
        support_record_id="src_work",
        support_json_pointer="/sources/0/content_markdown",
    )
    answer = _answer().model_copy(
        update={
            "claims": (
                ClaimEvidenceV3(
                    text="Tiểu thuyết kể về đời sống gia đình.",
                    citation_labels=("[C1]",),
                ),
            ),
            "citations": (
                CitationForReviewV3(
                    label="[C1]",
                    evidence="The novel concerns family life. Scope: work.",
                    source_id="src_work",
                    source_version_id="version_1",
                ),
            ),
            "rubric_context": _answer().rubric_context.model_copy(
                update={"required_facts": (fact,)}
            ),
        }
    )
    # The supplied semantic verdict is authoritative for entailment. Exact guards
    # must not override it with an English substring requirement.
    validate_model_judge_output_v3(_output(answer), answer)
    assert _coverage(answer).coverage == 1.0
    negated = answer.model_copy(
        update={
            "claims": (
                ClaimEvidenceV3(
                    text="Tiểu thuyết không nói về đời sống gia đình.",
                    citation_labels=("[C1]",),
                ),
            )
        }
    )
    assert _coverage(negated, supported=(False,)).coverage == 0.0


def _fact() -> RubricFactV3:
    return RubricFactV3(
        claim="rating",
        expected_value=5,
        evidence="products.jsonl/rating=5",
        support_kind="catalog_pointer",
        match_mode="numeric_exact",
        support_record_id="product_80",
        support_json_pointer="/rating",
    )


def _citation(evidence: str) -> CitationForReviewV3:
    return CitationForReviewV3(
        label="[C1]",
        evidence=evidence,
        source_id="catalog_product_80",
        source_version_id="cat_pinned",
    )


def _answer() -> BlindedAnswerV3:
    return BlindedAnswerV3(
        opaque_answer_id="answer_000000000000000000000001",
        prompt=("Rating?",),
        answer="Rating 5",
        citations=(_citation("snapshot_rating: 5.0 rating_5"),),
        claims=(ClaimEvidenceV3(text="Rating 5", citation_labels=("[C1]",)),),
        runtime_evidence=RuntimeRubricEvidenceV3(
            outcome="answered",
            planned_capabilities=(),
            successful_capabilities=(),
            successful_step_ids=(),
            plan_revision_count=1,
            final_revision_added_read_step_ids=(),
            final_revision_reused_step_ids=(),
        ),
        rubric_context=RubricContextV3(
            evaluator_contract="evidence_semantics_v2",
            answerability="answerable",
            required_response_mode="direct_answer",
            required_facts=(_fact(),),
            forbidden_claims=(),
            expected_action_outcome="complete",
            expected_dialogue_outcome="answered",
            allowed_capabilities=(),
            required_capabilities=(),
            forbidden_capabilities=(),
        ),
    )


def _output(
    answer: BlindedAnswerV3,
    *,
    supported: tuple[bool, ...] | None = None,
    support_score: float = 1.0,
) -> dict[str, object]:
    supported = supported if supported is not None else (True,) * len(answer.claims)
    return {
        "schema_version": "3.0",
        "claim_coverage_contract": "receipt_claim_citation_coverage_v1",
        "output_schema_sha256": model_judge_output_schema_sha256_v3(successor=True),
        "opaque_answer_id": answer.opaque_answer_id,
        "verdicts": [
            {
                "metric": metric,
                "score": support_score
                if metric
                in {
                    EvaluationMetricV3.CLAIM_SUPPORT,
                    EvaluationMetricV3.TASK_COMPLETION,
                }
                else 1.0,
                "rubric_fact_indices": [0],
                "citation_labels": [item.label for item in answer.citations],
            }
            for metric in SEMANTIC_JUDGE_METRICS_V3
        ],
        "claim_verdicts": [
            {
                "claim_index": index,
                "supported": verdict,
                "citation_labels": list(answer.claims[index].citation_labels)
                if verdict
                else [],
                "rubric_fact_indices": [0] if verdict else [],
            }
            for index, verdict in enumerate(supported)
        ],
    }


def _coverage(
    answer: BlindedAnswerV3, *, supported: tuple[bool, ...] | None = None
) -> ClaimCitationCoverageV3:
    output = validate_model_judge_output_v3(
        _output(answer, supported=supported, support_score=0.0), answer
    )
    result = claim_citation_coverage_from_output_v3(answer, output)
    assert result is not None
    return result
