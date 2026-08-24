"""Score a captured single-agent real-model artifact against the frozen corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from app.evaluation.models import EvalCase, EvaluationCorpus

ACTION_MAP = {
    "search_products": "product.search",
    "get_product_reviews": "review.summarize",
    "compare_products": "product.compare",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize(value: object) -> str:
    return " ".join(str(value).casefold().split())


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _retrieved_product_ids(result: dict[str, Any]) -> list[int]:
    product_ids: list[int] = []

    def append(value: object) -> None:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value not in product_ids
        ):
            product_ids.append(value)

    for event in result.get("tool_events", []):
        payload = event.get("result") or {}
        for product in payload.get("products", []) or []:
            if isinstance(product, dict):
                append(product.get("id", product.get("product_id")))
        product = payload.get("product")
        if isinstance(product, dict):
            append(product.get("id"))
        for review in payload.get("reviews", []) or []:
            if isinstance(review, dict):
                append(review.get("product_id"))
    return product_ids


def _score_case(case: EvalCase, result: dict[str, Any]) -> dict[str, Any]:
    answer = _normalize(result.get("answer", ""))
    assertions: list[dict[str, Any]] = []
    for assertion in case.answer_assertions:
        value = _normalize(assertion.value)
        if assertion.kind.value == "contains":
            passed = value in answer
        elif assertion.kind.value == "excludes":
            passed = value not in answer
        else:
            # The legacy single-agent contract does not expose provenance,
            # selected IDs, or structured error codes.
            passed = False
        assertions.append(
            {
                "kind": assertion.kind.value,
                "value": assertion.value,
                "passed": passed,
                "critical": assertion.critical,
            }
        )

    actual_actions = [
        ACTION_MAP[call["name"]]
        for call in result.get("tool_calls", [])
        if call.get("name") in ACTION_MAP
    ]
    expected_counts = Counter(
        {item.action: item.count for item in case.expected_actions}
    )
    actual_counts = Counter(actual_actions)
    action_true_positives = sum(
        min(count, expected_counts[action]) for action, count in actual_counts.items()
    )
    retrieved = set(_retrieved_product_ids(result))
    relevant = set(case.relevant_product_ids or ())
    retrieval_evaluated = case.relevant_product_ids is not None
    critical_pass = all(item["passed"] for item in assertions if item["critical"])
    critical_without_provenance_pass = all(
        item["passed"]
        for item in assertions
        if item["critical"] and item["kind"] != "provenance_at_least"
    )
    return {
        "case_id": case.case_id,
        "category": case.category.value,
        "api_status": result.get("status"),
        "task_success": result.get("status") == "success" and critical_pass,
        "task_success_without_provenance": (
            result.get("status") == "success" and critical_without_provenance_pass
        ),
        "assertions_passed": sum(item["passed"] for item in assertions),
        "assertion_count": len(assertions),
        "assertions": assertions,
        "actual_actions": actual_actions,
        "expected_actions": [item.model_dump() for item in case.expected_actions],
        "exact_plan": actual_counts == expected_counts,
        "action_true_positives": action_true_positives,
        "predicted_action_count": sum(actual_counts.values()),
        "expected_action_count": sum(expected_counts.values()),
        "retrieved_product_ids": sorted(retrieved),
        "relevant_product_ids": sorted(relevant),
        "retrieval_evaluated": retrieval_evaluated,
        "retrieval_true_positives": len(relevant & retrieved),
        "latency_ms": result.get("latency_ms"),
        "total_tokens": (result.get("usage_totals") or {}).get("total_tokens"),
    }


def _nearest_rank(values: list[float], quantile: float) -> float:
    return values[max(0, math.ceil(quantile * len(values)) - 1)]


def build_report(
    *,
    corpus: EvaluationCorpus,
    artifact: dict[str, Any],
    artifact_path: Path,
) -> dict[str, Any]:
    results = {item["case_id"]: item for item in artifact["results"]}
    if len(results) != len(corpus.cases) or set(results) != {
        case.case_id for case in corpus.cases
    }:
        raise ValueError("baseline artifact must cover every frozen case exactly once")

    case_scores = [_score_case(case, results[case.case_id]) for case in corpus.cases]
    latencies = sorted(float(item["latency_ms"]) for item in case_scores)
    retrieval_cases = [item for item in case_scores if item["retrieval_evaluated"]]
    no_tool_cases = [item for item in case_scores if item["expected_action_count"] == 0]
    assertion_count = sum(item["assertion_count"] for item in case_scores)
    assertion_passes = sum(item["assertions_passed"] for item in case_scores)
    action_true_positives = sum(item["action_true_positives"] for item in case_scores)
    predicted_actions = sum(item["predicted_action_count"] for item in case_scores)
    expected_actions = sum(item["expected_action_count"] for item in case_scores)
    retrieval_true_positives = sum(
        item["retrieval_true_positives"] for item in retrieval_cases
    )
    retrieved_count = sum(
        len(item["retrieved_product_ids"]) for item in retrieval_cases
    )
    relevant_count = sum(len(item["relevant_product_ids"]) for item in retrieval_cases)
    successful_api_turns = sum(item["api_status"] == "success" for item in case_scores)
    metrics = {
        "api_turn_success_rate": {
            "numerator": successful_api_turns,
            "denominator": len(case_scores),
            "value": _ratio(successful_api_turns, len(case_scores)),
        },
        "task_success_rate_with_frozen_assertions": {
            "numerator": sum(item["task_success"] for item in case_scores),
            "denominator": len(case_scores),
            "value": _ratio(
                sum(item["task_success"] for item in case_scores),
                len(case_scores),
            ),
        },
        "task_success_rate_without_provenance_assertions": {
            "numerator": sum(
                item["task_success_without_provenance"] for item in case_scores
            ),
            "denominator": len(case_scores),
            "value": _ratio(
                sum(item["task_success_without_provenance"] for item in case_scores),
                len(case_scores),
            ),
        },
        "answer_assertion_accuracy": {
            "numerator": assertion_passes,
            "denominator": assertion_count,
            "value": _ratio(assertion_passes, assertion_count),
        },
        "tool_selection_precision": {
            "numerator": action_true_positives,
            "denominator": predicted_actions,
            "value": _ratio(action_true_positives, predicted_actions),
        },
        "tool_selection_recall": {
            "numerator": action_true_positives,
            "denominator": expected_actions,
            "value": _ratio(action_true_positives, expected_actions),
        },
        "exact_plan_rate": {
            "numerator": sum(item["exact_plan"] for item in case_scores),
            "denominator": len(case_scores),
            "value": _ratio(
                sum(item["exact_plan"] for item in case_scores),
                len(case_scores),
            ),
        },
        "no_tool_correctness": {
            "numerator": sum(not item["actual_actions"] for item in no_tool_cases),
            "denominator": len(no_tool_cases),
            "value": _ratio(
                sum(not item["actual_actions"] for item in no_tool_cases),
                len(no_tool_cases),
            ),
        },
        "retrieval_precision": {
            "numerator": retrieval_true_positives,
            "denominator": retrieved_count,
            "value": _ratio(retrieval_true_positives, retrieved_count),
        },
        "retrieval_recall": {
            "numerator": retrieval_true_positives,
            "denominator": relevant_count,
            "value": _ratio(retrieval_true_positives, relevant_count),
        },
        "provenance_case_coverage": {
            "numerator": 0,
            "denominator": sum(
                item["expected_action_count"] > 0 for item in case_scores
            ),
            "value": 0.0,
        },
        "latency_p50_ms": {
            "value": _nearest_rank(latencies, 0.50),
            "sample_size": len(latencies),
        },
        "latency_p95_ms": {
            "value": _nearest_rank(latencies, 0.95),
            "sample_size": len(latencies),
        },
        "token_usage": {
            "value": sum(int(item["total_tokens"] or 0) for item in case_scores),
            "sample_size": len(case_scores),
        },
        "llm_cost_usd": {
            "value": None,
            "unavailable_reason": (
                "Provider pricing was not captured; no cost estimate is claimed."
            ),
        },
    }
    return {
        "schema_version": "1.0",
        "system_id": artifact["system_id"],
        "baseline_id": artifact["baseline_id"],
        "captured_at": artifact["captured_at"],
        "git_revision": artifact["git_revision"],
        "model": artifact["model"],
        "dataset_id": artifact["dataset_id"],
        "dataset_sha256": _sha256(
            Path(__file__).resolve().parents[1] / "evaluation" / "cases.v1.json"
        ),
        "sample_counts": artifact["sample_counts"],
        "artifact_sha256": _sha256(artifact_path),
        "case_count": len(case_scores),
        "metrics": metrics,
        "case_scores": case_scores,
        "limitations": [
            (
                "Đây là một lần chạy real-model trên sample data, không phải "
                "human semantic evaluation."
            ),
            (
                "Single-agent main không phát provenance nên provenance coverage "
                "bằng 0 theo rubric Multi-Agent."
            ),
            (
                "Failure-injection cases được gửi như prompt bình thường; không "
                "thể ép main single-agent vào cùng failure dispatcher."
            ),
            "Không ghi chi phí vì pricing/provider billing không được capture.",
            "Không suy diễn routing intent ẩn từ tool calls.",
        ],
    }


def _metric_value(metric: dict[str, Any]) -> str:
    value = metric.get("value")
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Single-Agent Real-Model Baseline v1",
        "",
        f"- Model: `{report['model']}`",
        f"- Main revision: `{report['git_revision']}`",
        f"- Dataset: `{report['dataset_id']}`",
        f"- Dataset SHA-256: `{report['dataset_sha256']}`",
        f"- Captured cases: `{report['case_count']}`",
        f"- Artifact SHA-256: `{report['artifact_sha256']}`",
        "",
        "Đây là baseline single-agent chạy bằng API thật trên cùng frozen corpus.",
        "Không dùng artifact này để claim hơn-kém nếu chưa có paired run cùng model,",
        "prompt, runtime và rubric.",
        "",
        "## Metrics",
        "",
        "| Metric | Value | Numerator/denominator |",
        "| --- | ---: | ---: |",
    ]
    for name, metric in report["metrics"].items():
        note = ""
        if metric.get("numerator") is not None:
            note = f"{metric['numerator']}/{metric['denominator']}"
        elif metric.get("unavailable_reason"):
            note = metric["unavailable_reason"]
        lines.append(f"| {name} | {_metric_value(metric)} | {note} |")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Score a captured single-agent real-model artifact."
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=project_root
        / "evaluation"
        / "results"
        / "baseline-single-agent-v1"
        / "observations.json",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=project_root / "evaluation" / "cases.v1.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "evaluation" / "results" / "baseline-single-agent-v1",
    )
    arguments = parser.parse_args()
    corpus = EvaluationCorpus.model_validate_json(
        arguments.cases.read_text(encoding="utf-8")
    )
    artifact = json.loads(arguments.artifact.read_text(encoding="utf-8"))
    report = build_report(
        corpus=corpus,
        artifact=artifact,
        artifact_path=arguments.artifact,
    )
    arguments.output.mkdir(parents=True, exist_ok=True)
    (arguments.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (arguments.output / "report.md").write_text(
        render_markdown(report),
        encoding="utf-8",
    )
    print(f"Baseline report written to {arguments.output.resolve()}")


if __name__ == "__main__":
    main()
