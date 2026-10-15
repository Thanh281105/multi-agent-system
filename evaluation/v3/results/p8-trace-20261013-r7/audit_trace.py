from __future__ import annotations

import collections
import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
AUDIT_DIR = Path(__file__).resolve().parent
P7_DIR = ROOT / "output/evaluation-v3/p7-trace-20261013-r7"
P8_DIR = ROOT / "output/evaluation-v3/heldout-trace-20261013-r7"
CHECKPOINT = P8_DIR / "heldout-checkpoint.v3.jsonl"
SCHEDULE = P8_DIR / "heldout-schedule.p8.json"
EXECUTION_PLAN = P8_DIR / "heldout-execution-plan.p8.json"
PROTOCOL = P7_DIR / "protocol.v3.json"
P7_CHECKPOINT = P7_DIR / "pilot-checkpoint.v3.jsonl"
GOLD = ROOT / "evaluation/v3/review-successor-v1/gold.v3.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def quantile_type7(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile needs at least one observation")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def counter_dict(values: collections.Counter[str]) -> dict[str, int]:
    return dict(sorted(values.items()))


events: list[dict[str, Any]] = [
    json.loads(line)
    for line in CHECKPOINT.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
started = [event for event in events if event["event"] == "started"]
completed_events = [event for event in events if event["event"] == "completed"]
receipts = [event["payload"]["receipt"] for event in completed_events]
protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
schedule = json.loads(SCHEDULE.read_text(encoding="utf-8"))
execution_plan = json.loads(EXECUTION_PLAN.read_text(encoding="utf-8"))
gold_rows = json.loads(GOLD.read_text(encoding="utf-8"))["conversations"]
heldout_gold = {
    row["conversation_id"]: row for row in gold_rows if row["split"] == "held_out"
}

expected_protocol_sha = sha256(PROTOCOL)
expected_schedule_sha = sha256(SCHEDULE)
if len(started) != 720 or len(completed_events) != 720 or len(receipts) != 720:
    raise SystemExit(f"expected 720 started and completed cells; got {len(started)}/{len(completed_events)}")
if execution_plan.get("package7_protocol_sha256") != expected_protocol_sha:
    raise SystemExit("P8 schedule is not bound to the P7 protocol hash")
if execution_plan.get("heldout_schedule_sha256") != expected_schedule_sha:
    raise SystemExit("P8 schedule self-hash mismatch")
if len(schedule) != 720:
    raise SystemExit(f"expected 720 scheduled cells; got {len(schedule)}")
gold_payload = json.loads(GOLD.read_text(encoding="utf-8"))
if canonical_sha256(gold_payload) != protocol["assets"]["gold_sha256"]:
    raise SystemExit("P8 gold does not match the frozen protocol hash")
if {receipt["identity"]["case_id"] for receipt in receipts} != set(heldout_gold):
    raise SystemExit("P8 receipt cases differ from the frozen held-out gold roster")
if any(receipt.get("protocol_sha256") != expected_protocol_sha for receipt in receipts):
    raise SystemExit("a P8 receipt has a different protocol binding")

variants: dict[str, Any] = {}
for variant_id in sorted({receipt["identity"]["variant_id"] for receipt in receipts}):
    rows = [receipt for receipt in receipts if receipt["identity"]["variant_id"] == variant_id]
    outcomes: collections.Counter[str] = collections.Counter()
    stages: collections.Counter[str] = collections.Counter()
    errors: collections.Counter[str] = collections.Counter()
    fallbacks: collections.Counter[str] = collections.Counter()
    retrieval_count_dist: collections.Counter[str] = collections.Counter()
    fallback_by_fact_count: dict[int, dict[str, Any]] = {}
    fallback_by_category: dict[str, dict[str, Any]] = {}
    citation_structure = {
        "claims": 0,
        "claims_with_citation_ids": 0,
        "citation_references": 0,
        "citation_references_resolved": 0,
        "citations": 0,
        "citations_with_evidence_resolved": 0,
        "emitted_evidence_items": 0,
    }
    observation_latency: list[float] = []
    user_turn_count = 0
    generation_calls = 0
    retry_count = 0
    input_tokens = 0
    output_tokens = 0
    known_cost = Decimal("0")

    for receipt in rows:
        turns = receipt["turn_results"]
        gold_case = heldout_gold[receipt["identity"]["case_id"]]
        fact_count = len(gold_case["required_fact_blueprints"])
        category = gold_case["category"]
        fact_bucket = fallback_by_fact_count.setdefault(
            fact_count,
            {"cases": set(), "work_groups": set(), "observations": 0, "user_turns": 0, "knowledge_retrieval_turns": 0, "missing_required_knowledge_fallback_turns": 0},
        )
        category_bucket = fallback_by_category.setdefault(
            category,
            {"cases": set(), "work_groups": set(), "observations": 0, "user_turns": 0, "knowledge_retrieval_turns": 0, "missing_required_knowledge_fallback_turns": 0},
        )
        for bucket in (fact_bucket, category_bucket):
            bucket["cases"].add(gold_case["conversation_id"])
            bucket["work_groups"].add(receipt["identity"]["work_group_id"])
            bucket["observations"] += 1
            bucket["user_turns"] += len(turns)
        user_turn_count += len(turns)
        observation_latency.append(sum(float(turn["elapsed_seconds"]) for turn in turns))
        generation_calls += int(receipt["totals"]["generation_calls"])
        retry_count += int(receipt["totals"]["retry_count"])
        input_tokens += int(receipt["totals"]["input_tokens"])
        output_tokens += int(receipt["totals"]["output_tokens"])
        known_cost += Decimal(receipt["totals"]["known_cost_usd"])
        for turn in turns:
            payload = turn["result_payload"]
            outcomes[payload["outcome"]] += 1
            usage = payload.get("usage") or {}
            retrieval_count = int(usage.get("knowledge_retrievals", 0))
            retrieval_count_dist[str(retrieval_count)] += 1
            if retrieval_count:
                fact_bucket["knowledge_retrieval_turns"] += 1
                category_bucket["knowledge_retrieval_turns"] += 1
            if any(
                call.get("fallback_reason") == "answer_repair_missing_required_knowledge"
                for call in turn["model_calls"]
            ):
                fact_bucket["missing_required_knowledge_fallback_turns"] += 1
                category_bucket["missing_required_knowledge_fallback_turns"] += 1

            result = payload.get("result") or {}
            claims = result.get("claims") or []
            citations = result.get("citations") or []
            emitted_evidence = result.get("evidence") or []
            citation_ids = {
                item.get("citation_id")
                for item in citations
                if isinstance(item, dict) and item.get("citation_id")
            }
            evidence_ids = {
                item.get("evidence_id")
                for item in emitted_evidence
                if isinstance(item, dict) and item.get("evidence_id")
            }
            citation_structure["claims"] += len(claims)
            citation_structure["claims_with_citation_ids"] += sum(
                bool(item.get("citation_ids"))
                for item in claims
                if isinstance(item, dict)
            )
            citation_structure["citation_references"] += sum(
                len(item.get("citation_ids") or [])
                for item in claims
                if isinstance(item, dict)
            )
            citation_structure["citation_references_resolved"] += sum(
                citation_id in citation_ids
                for item in claims
                if isinstance(item, dict)
                for citation_id in item.get("citation_ids") or []
            )
            citation_structure["citations"] += len(citations)
            citation_structure["citations_with_evidence_resolved"] += sum(
                item.get("evidence_id") in evidence_ids
                for item in citations
                if isinstance(item, dict)
            )
            citation_structure["emitted_evidence_items"] += len(emitted_evidence)
            for call in turn["model_calls"]:
                stages[call["stage"]] += 1
                if call.get("error_code"):
                    errors[call["error_code"]] += 1
                if call.get("fallback_reason"):
                    fallbacks[call["fallback_reason"]] += 1

    variants[variant_id] = {
        "observations": len(rows),
        "cases": len({receipt["identity"]["case_id"] for receipt in rows}),
        "work_groups": len({receipt["identity"]["work_group_id"] for receipt in rows}),
        "user_turns": user_turn_count,
        "generation_calls": generation_calls,
        "retry_count": retry_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "known_cost_usd": format(known_cost, "f"),
        "observation_latency_seconds": {
            "definition": "sum of elapsed_seconds across turns inside each observation; type-7 percentile",
            "p50": round(quantile_type7(observation_latency, 0.50), 6),
            "p95": round(quantile_type7(observation_latency, 0.95), 6),
        },
        "user_turn_outcomes": counter_dict(outcomes),
        "knowledge_retrieval_count_by_user_turn": counter_dict(retrieval_count_dist),
        "citation_structure_only": citation_structure,
        "answer_repair_missing_required_knowledge_by_gold_fact_count": {
            str(key): {
                **{name: value for name, value in item.items() if name not in {"cases", "work_groups"}},
                "unique_cases": len(item["cases"]),
                "work_groups": len(item["work_groups"]),
            }
            for key, item in sorted(fallback_by_fact_count.items())
        },
        "answer_repair_missing_required_knowledge_by_gold_category": {
            key: {
                **{name: value for name, value in item.items() if name not in {"cases", "work_groups"}},
                "unique_cases": len(item["cases"]),
                "work_groups": len(item["work_groups"]),
            }
            for key, item in sorted(fallback_by_category.items())
        },
        "model_call_stages": counter_dict(stages),
        "model_error_codes": counter_dict(errors),
        "fallback_reasons": counter_dict(fallbacks),
    }

adaptive = variants["ma_adaptive_rag"]
sa = variants["sa_shared_tools_rag"]
fixed = variants["ma_fixed_rag"]

def pct_delta(left: Decimal | float, right: Decimal | float) -> float:
    return round((float(left) / float(right) - 1.0) * 100.0, 3)

p8_total_cost = sum((Decimal(item["known_cost_usd"]) for item in variants.values()), Decimal("0"))
summary = {
    "classification": "development_trace_only_not_independent_validation",
    "run_id": "run_p8_trace_20261013_r7",
    "sut_git_revision": protocol["assets"]["sut_git_revision"],
    "protocol_sha256": expected_protocol_sha,
    "p7_schedule_sha256": sha256(P7_DIR / "pilot-schedule.v3.json"),
    "p7_checkpoint_sha256": sha256(P7_CHECKPOINT),
    "p8_schedule_sha256": expected_schedule_sha,
    "p8_checkpoint_sha256": sha256(CHECKPOINT),
    "p8_checkpoint_bytes": CHECKPOINT.stat().st_size,
    "p8_started_cells": len(started),
    "p8_completed_cells": len(completed_events),
    "variants": variants,
    "trace_only_comparisons": {
        "adaptive_rag_vs_sa_rag": {
            "generation_calls_delta_percent": pct_delta(adaptive["generation_calls"], sa["generation_calls"]),
            "known_cost_delta_percent": pct_delta(Decimal(adaptive["known_cost_usd"]), Decimal(sa["known_cost_usd"])),
            "p95_observation_latency_delta_percent": pct_delta(adaptive["observation_latency_seconds"]["p95"], sa["observation_latency_seconds"]["p95"]),
        },
        "adaptive_rag_vs_fixed_rag": {
            "generation_calls_delta_percent": pct_delta(adaptive["generation_calls"], fixed["generation_calls"]),
            "known_cost_delta_percent": pct_delta(Decimal(adaptive["known_cost_usd"]), Decimal(fixed["known_cost_usd"])),
            "p95_observation_latency_delta_percent": pct_delta(adaptive["observation_latency_seconds"]["p95"], fixed["observation_latency_seconds"]["p95"]),
        },
        "p8_known_cost_usd_all_variants": format(p8_total_cost, "f"),
    },
    "not_measured": [
        "semantic correctness",
        "claim-to-source support",
        "citation precision or coverage",
        "document recall",
        "human agreement",
        "independent confirmation performance",
        "document recall because retrieval-hit source IDs were not persisted in this run",
    ],
    "analysis_method": {
        "observations_per_variant": 180,
        "work_groups_per_variant": 27,
        "repeats_per_case": 3,
        "latency_percentile": "Hyndman-Fan type 7 over 180 observation sums",
        "cost_source": "checkpoint receipt totals.known_cost_usd",
        "token_source": "checkpoint receipt totals.input_tokens/output_tokens",
        "outcome_counts_unit": "user turns recorded within observations; not independent tasks",
        "gold_fact_count_unit": "case-level required_fact_blueprints; repeated observations are descriptive only",
        "citation_structure_only": "ID/link integrity, not semantic source support or citation precision",
        "no_semantic_judgment_was_run": True,
    },
}
(AUDIT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"summary": str(AUDIT_DIR / "summary.json"), "variants": variants, "comparisons": summary["trace_only_comparisons"]}, ensure_ascii=False, indent=2))
