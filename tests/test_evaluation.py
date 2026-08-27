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
    _sut_source_manifest,
    load_baseline_manifest,
    load_corpus,
    run_evaluation,
    validate_corpus,
    write_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "evaluation" / "cases.v1.json"
BASELINE_PATH = PROJECT_ROOT / "evaluation" / "baselines" / "single_agent.v1.json"
REAL_BASELINE_ARTIFACT = (
    PROJECT_ROOT
    / "evaluation"
    / "results"
    / "baseline-single-agent-v1"
    / "observations.json"
)
REAL_MULTI_ARTIFACT = (
    PROJECT_ROOT
    / "evaluation"
    / "results"
    / "real-multi-agent-v1"
    / "observations.json"
)
REAL_MULTI_REPORT = REAL_MULTI_ARTIFACT.with_name("report.json")


def canonical_sha256(path: Path) -> str:
    """Match artifact scorer hashes regardless of checkout line endings."""

    payload = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def test_artifact_hash_is_line_ending_stable(tmp_path: Path) -> None:
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{"stable": true}\n')
    crlf.write_bytes(b'{"stable": true}\r\n')

    assert canonical_sha256(lf) == canonical_sha256(crlf)


def test_sut_source_manifest_is_line_ending_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "app"
    source.mkdir()
    module = source / "module.py"
    module.write_bytes(b"VALUE = 1\n")
    monkeypatch.setattr("app.evaluation.runner._default_project_root", lambda: tmp_path)

    lf_hash, lf_files = _sut_source_manifest()

    module.write_bytes(b"VALUE = 1\r\n")
    crlf_hash, crlf_files = _sut_source_manifest()

    assert crlf_hash == lf_hash
    assert crlf_files == lf_files == ("app/module.py",)


def test_frozen_corpus_is_balanced_and_real_baseline_is_bound() -> None:
    corpus = load_corpus(CASES_PATH)
    baseline = load_baseline_manifest(BASELINE_PATH)

    assert len(corpus.cases) == 28
    assert Counter(case.category for case in corpus.cases) == {
        category: 4 for category in EvaluationCategory
    }
    assert baseline.status == "real_model_captured"
    assert baseline.captured_case_count == 28
    assert baseline.model == "gpt-5.4-mini"
    assert baseline.artifact_path == (
        "evaluation/results/baseline-single-agent-v1/observations.json"
    )
    assert baseline.artifact_sha256
    assert baseline.prompt_sha256
    assert baseline.tool_schema_sha256
    assert (
        baseline.dataset_sha256 == hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
    )


def test_baseline_unavailable_cannot_claim_model_evidence() -> None:
    with pytest.raises(ValidationError, match="cannot claim captured model evidence"):
        BaselineManifest(
            baseline_id="dishonest_baseline",
            status="baseline_unavailable",
            reason="Not captured.",
            captured_case_count=1,
            model="provider-model",
        )


def test_real_baseline_artifact_covers_frozen_corpus() -> None:
    baseline = load_baseline_manifest(BASELINE_PATH)
    artifact = json.loads(REAL_BASELINE_ARTIFACT.read_text(encoding="utf-8"))

    assert REAL_BASELINE_ARTIFACT.is_file()
    assert artifact["system_id"] == "single_agent_real_main"
    assert artifact["case_count"] == baseline.captured_case_count == 28
    assert len(artifact["results"]) == 28
    assert len({item["case_id"] for item in artifact["results"]}) == 28
    assert all(item["status"] == "success" for item in artifact["results"])
    assert canonical_sha256(REAL_BASELINE_ARTIFACT) == baseline.artifact_sha256


def test_real_multi_agent_artifact_is_frozen_and_scoreable() -> None:
    corpus = load_corpus(CASES_PATH)
    artifact = json.loads(REAL_MULTI_ARTIFACT.read_text(encoding="utf-8"))
    report = json.loads(REAL_MULTI_REPORT.read_text(encoding="utf-8"))

    assert REAL_MULTI_ARTIFACT.is_file()
    assert REAL_MULTI_REPORT.is_file()
    assert artifact["system_id"] == "multi_agent_real"
    assert artifact["case_count"] == len(corpus.cases) == 28
    assert artifact["repeats"] == 1
    assert artifact["case_ids"] == [case.case_id for case in corpus.cases]
    assert len(artifact["results"]) == 28
    assert (
        len({(item["case_id"], item["repetition"]) for item in artifact["results"]})
        == 28
    )
    assert all(
        item["observation"]["system_id"] == "multi_agent_real"
        for item in artifact["results"]
    )
    assert report["system_id"] == "multi_agent_real"
    assert report["artifact_sha256"] == canonical_sha256(REAL_MULTI_ARTIFACT)
    assert report["metrics"]["task_success_rate"]["value"] == 1
    assert report["metrics"]["answer_assertion_accuracy"]["value"] == 1
    assert report["metrics"]["retrieval_f1"]["value"] == 1
    assert report["comparison"]["rows"]
    assert "OPENAI_API_KEY" not in REAL_MULTI_ARTIFACT.read_text(encoding="utf-8")


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
    assert report.comparison_status == "real_model_captured"
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
    expected_sut_hash, expected_sut_files = _sut_source_manifest()
    assert report.schema_version == "1.1"
    assert report.sut_source_sha256 == expected_sut_hash
    assert report.sut_source_files == expected_sut_files
    assert all(
        path.startswith("app/") and path.endswith(".py") for path in expected_sut_files
    )
    assert tuple(sorted(expected_sut_files)) == expected_sut_files

    report_json = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    observations_csv = (tmp_path / "observations.csv").read_text(encoding="utf-8")
    report_markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert report_json["dataset_sha256"] == dataset_hash
    assert report_json["sut_source_sha256"] == expected_sut_hash
    assert report_json["sut_source_files"] == list(expected_sut_files)
    assert observations_csv.count("\n") == 29
    assert "SUT source manifest SHA-256" in report_markdown
    assert "real_model_captured" in report_markdown
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
