import json
from pathlib import Path

import pytest

from app.evaluation.rejudge_v4 import (
    INSTRUCTIONS,
    SourceStore,
    build_fact_context,
    build_requests,
    request_key,
    summarize,
)

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "snapshots" / "tiki-books-v4-eval"
KNOWLEDGE = ROOT / "data" / "knowledge" / "books-v1" / "sources.json"
CASES = ROOT / "evaluation" / "v4_fair_benchmark_cases.json"
RESULTS = ROOT / "output" / "evaluation-v4" / "full_24_benchmark_results.json"


@pytest.fixture(scope="module")
def store() -> SourceStore:
    return SourceStore(SNAPSHOT, KNOWLEDGE)


def test_context_catalog_record_found(store: SourceStore) -> None:
    fact = {
        "source_provenance": {
            "source_type": "catalog_product",
            "record_id": "184419723",
            "exact_value": "Sapiens Lược Sử Loài Người (Tái Bản 2022)",
        }
    }
    context = build_fact_context(fact, store)
    assert "Tái Bản 2022" in context
    assert "không tìm thấy" not in context


def test_context_review_found(store: SourceStore) -> None:
    fact = {
        "source_provenance": {
            "source_type": "customer_review",
            "record_id": "101325467:12230846",
            "exact_quote": "Hơi buồn vì cuốn sách đầu tiên",
        }
    }
    assert "mất trang" in build_fact_context(fact, store)


def test_context_knowledge_source_found(store: SourceStore) -> None:
    fact = {
        "source_provenance": {
            "source_type": "knowledge_excerpt",
            "record_id": "src_morisaki_nlv",
            "exact_quote": "Yagisawa Satoshi",
        }
    }
    context = build_fact_context(fact, store)
    assert "không tìm thấy" not in context
    assert "Yagisawa" in context


def test_context_for_policy_fact_without_artifact(store: SourceStore) -> None:
    fact = {
        "source_provenance": {
            "source_type": "guardrail_policy",
            "record_id": "policy_read_only_boundaries",
            "policy_rule": "Hệ thống không có quyền mutation.",
        }
    }
    assert "không có quyền mutation" in build_fact_context(fact, store)


def test_context_missing_record_is_explicit(store: SourceStore) -> None:
    fact = {
        "source_provenance": {
            "source_type": "catalog_product",
            "record_id": "does-not-exist",
        }
    }
    assert "không tìm thấy bản ghi sản phẩm" in build_fact_context(fact, store)


def test_prompt_demands_exact_blueprint_value() -> None:
    assert "(Tái Bản 2022)" in INSTRUCTIONS
    assert "unsupported_extra_claims" in INSTRUCTIONS


@pytest.mark.skipif(
    not RESULTS.exists(),
    reason="kết quả v4 nằm trong output/ (gitignored) và không có trên máy này",
)
def test_build_requests_count_is_144_on_real_results() -> None:
    requests = build_requests(RESULTS, CASES, SNAPSHOT, knowledge_path=KNOWLEDGE)
    assert len(requests) == 144
    key = request_key("fair_tier2_cross_06", "multi_agent", 0)
    assert key in requests
    payload = json.loads(requests[key].input_text)
    assert payload["facts"] and all("source_context" in f for f in payload["facts"])
    assert not any(
        "không tìm thấy" in f["source_context"]
        for r in requests.values()
        for f in json.loads(r.input_text)["facts"]
    )


def test_summarize_flags_changed_cases(tmp_path: Path) -> None:
    cases = {
        "cases": [
            {"case_id": "c1", "required_facts": [{"fact_id": "a"}, {"fact_id": "b"}]}
        ]
    }
    results = [
        {
            "case_id": "c1",
            "single_agent": {"repeats": [{"repeat": 0, "score": 0.5}]},
            "multi_agent": {"repeats": [{"repeat": 0, "score": 1.0}]},
        }
    ]
    cases_path = tmp_path / "cases.json"
    results_path = tmp_path / "results.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")
    results_path.write_text(json.dumps(results), encoding="utf-8")
    cache = {
        "c1|single_agent|0": {
            "error": None,
            "output": {
                "verdicts": [
                    {"fact_id": "a", "supported": True, "reason": ""},
                    {"fact_id": "b", "supported": True, "reason": ""},
                ],
                "unsupported_extra_claims": ["bịa"],
            },
        },
        "c1|multi_agent|0": {"error": "schema", "output": None},
    }
    summary = summarize(results_path, cases_path, cache)
    assert summary["changed_repeats"] == [
        {"key": "c1|single_agent|0", "old": 0.5, "new": 1.0}
    ]
    assert summary["unsupported_extra_claims"] == [
        {"key": "c1|single_agent|0", "claim": "bịa"}
    ]
    assert summary["errors"] == 1
    assert summary["overall"]["single_agent"] == {"old": 0.5, "new": 1.0}
