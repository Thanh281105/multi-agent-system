"""Offline benchmark integrity, arithmetic, and end-to-end regression tests."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts import TaskStatus
from app.evaluation.metrics import aggregate_metrics, score_case
from app.evaluation.models import (
    AnswerAssertion,
    AssertionKind,
    BaselineManifest,
    EvalCase,
    EvaluationCategory,
    EvaluationObservation,
    ExpectedAction,
)
from app.evaluation.runner import (
    load_baseline_manifest,
    load_corpus,
    run_evaluation,
    validate_corpus,
    write_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "evaluation" / "cases.v1.json"
BASELINE_PATH = PROJECT_ROOT / "evaluation" / "baselines" / "single_agent.v1.json"


def test_frozen_corpus_is_balanced_and_baseline_is_honest() -> None:
    corpus = load_corpus(CASES_PATH)
    baseline = load_baseline_manifest(BASELINE_PATH)

    assert len(corpus.cases) == 28
    assert Counter(case.category for case in corpus.cases) == {
        category: 4 for category in EvaluationCategory
    }
    assert baseline.status == "baseline_unavailable"
    assert baseline.captured_case_count == 0
    assert "chưa có đủ 28 quan sát mô hình thật" in baseline.reason


def test_baseline_unavailable_cannot_claim_model_evidence() -> None:
    with pytest.raises(ValidationError, match="cannot claim captured model evidence"):
        BaselineManifest(
            baseline_id="dishonest_baseline",
            status="baseline_unavailable",
            reason="Not captured.",
            captured_case_count=1,
            model="provider-model",
        )


def test_multiset_metrics_preserve_duplicates_and_empty_retrieval_semantics() -> None:
    case = EvalCase(
        case_id="metric_duplicate_actions",
        category=EvaluationCategory.COMPLEX,
        message="So sánh A với B",
        accepted_intents=("product.compare",),
        expected_actions=(ExpectedAction(action="product.search", count=2),),
        accepted_statuses=(TaskStatus.SUCCESS,),
        relevant_product_ids=(),
        answer_assertions=(
            AnswerAssertion(kind=AssertionKind.CONTAINS, value="dữ liệu mẫu"),
        ),
    )
    observation = EvaluationObservation(
        case_id=case.case_id,
        category=case.category,
        repetition=0,
        status=TaskStatus.SUCCESS,
        predicted_intent="product.compare",
        actions=("product.search",),
        answer="Kết quả từ dữ liệu mẫu.",
        provenance_count=1,
        agent_attempts=1,
        agent_failures=0,
        latency_ms=2,
    )

    score = score_case(case, observation)

    assert score.action_precision == 1
    assert score.action_recall == 0.5
    assert score.action_f1 == pytest.approx(2 / 3)
    assert score.exact_plan is False
    assert score.empty_retrieval_correct is True


def test_zero_denominators_are_reported_as_unavailable() -> None:
    case = EvalCase(
        case_id="metric_no_tool_case",
        category=EvaluationCategory.IRRELEVANT,
        message="Viết thơ",
        accepted_intents=("general.unsupported",),
        accepted_statuses=(TaskStatus.SUCCESS,),
        relevant_product_ids=(),
        answer_assertions=(
            AnswerAssertion(kind=AssertionKind.CONTAINS, value="chưa gọi tool"),
        ),
    )
    observation = EvaluationObservation(
        case_id=case.case_id,
        category=case.category,
        repetition=0,
        status=TaskStatus.SUCCESS,
        predicted_intent="general.unsupported",
        answer="Ngoài phạm vi; chưa gọi tool.",
        provenance_count=0,
        agent_attempts=0,
        agent_failures=0,
        latency_ms=1,
    )
    metrics = aggregate_metrics(
        [score_case(case, observation)],
        [observation],
    )

    assert metrics["tool_selection_precision"].value is None
    assert metrics["tool_selection_precision"].unavailable_reason
    assert metrics["no_tool_correctness"].value == 1
    assert metrics["token_usage"].value is None
    assert "production token usage was not measured" in (
        metrics["token_usage"].unavailable_reason or ""
    )


def test_corpus_rejects_unknown_canonical_action() -> None:
    corpus = load_corpus(CASES_PATH)
    invalid_case = corpus.cases[0].model_copy(
        update={
            "expected_actions": (ExpectedAction(action="unknown.execute", count=1),)
        }
    )
    invalid_corpus = corpus.model_copy(
        update={"cases": (invalid_case, *corpus.cases[1:])}
    )

    with pytest.raises(ValueError, match="unknown canonical actions"):
        validate_corpus(invalid_corpus)


@pytest.mark.asyncio
async def test_complete_offline_benchmark_passes_frozen_gold_and_writes_reports(
    tmp_path: Path,
) -> None:
    corpus = load_corpus(CASES_PATH)
    baseline = load_baseline_manifest(BASELINE_PATH)
    dataset_hash = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()

    report = await run_evaluation(
        corpus=corpus,
        baseline=baseline,
        dataset_sha256=dataset_hash,
        repeats=1,
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    write_report(report, tmp_path)

    assert len(report.case_scores) == 28
    assert all(item.task_success for item in report.case_scores)
    assert report.metrics["routing_accuracy"].value == 1
    assert report.metrics["tool_selection_f1"].value == 1
    assert report.metrics["task_success_rate"].value == 1
    assert report.metrics["answer_assertion_accuracy"].value == 1
    assert report.metrics["retrieval_f1"].value == 1
    assert report.metrics["partial_recovery_rate"].value == 1
    assert report.comparison_status == "baseline_unavailable"
    assert report.sample_counts == {"shops": 5, "products": 30, "reviews": 150}
    assert report.random_seed == 42
    assert (
        report.sample_seed_sha256
        == hashlib.sha256(
            (PROJECT_ROOT / "app" / "db" / "seed.py").read_bytes()
        ).hexdigest()
    )
    assert report.python_version
    assert report.runtime_platform

    report_json = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    observations_csv = (tmp_path / "observations.csv").read_text(encoding="utf-8")
    report_markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert report_json["dataset_sha256"] == dataset_hash
    assert observations_csv.count("\n") == 29
    assert "baseline_unavailable" in report_markdown
    assert "không đại diện thị trường thật" in report_markdown


@pytest.mark.asyncio
async def test_evaluation_bounds_repeats_and_case_slice() -> None:
    corpus = load_corpus(CASES_PATH)
    baseline = load_baseline_manifest(BASELINE_PATH)
    dataset_hash = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="repeats"):
        await run_evaluation(
            corpus=corpus,
            baseline=baseline,
            dataset_sha256=dataset_hash,
            repeats=0,
        )
    with pytest.raises(ValueError, match="max_cases"):
        await run_evaluation(
            corpus=corpus,
            baseline=baseline,
            dataset_sha256=dataset_hash,
            repeats=1,
            max_cases=0,
        )
