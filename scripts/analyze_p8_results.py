import json
from pathlib import Path
from collections import defaultdict
import statistics

def run_analysis(journal_path=None, prep_path=None):
    root = Path(__file__).resolve().parents[1]
    if journal_path is None:
        journal_path = root / "output" / "evaluation-v3" / "heldout-live-v3" / "p8-preparation" / "heldout-judge.p8.jsonl"
    if prep_path is None:
        prep_path = root / "output" / "evaluation-v3" / "heldout-live-v3" / "p8-preparation" / "heldout-preparation.p8.json"

    prep = json.loads(prep_path.read_text(encoding="utf-8"))
    unblinding = prep["blinded"]["unblinding_key"]
    answer_meta = {e["opaque_answer_id"]: e for e in unblinding.get("entries", [])}

    lines = journal_path.read_text(encoding="utf-8").strip().split("\n")
    terminals = [json.loads(l) for l in lines if json.loads(l).get("record_type") == "terminal"]

    total = len(terminals)
    print(f"=== P8 SCIENTIFIC COMPARATIVE ANALYSIS (Evaluated: {total}/720 cells) ===")

    records = []
    for t in terminals:
        op_id = t["artifact"]["opaque_answer_id"]
        meta = answer_meta.get(op_id, {})
        scores = t["artifact"]["scores"]
        records.append({
            "opaque_id": op_id,
            "conversation_id": meta.get("conversation_id"),
            "variant_id": meta.get("variant_id"),
            "repetition": meta.get("repetition"),
            "scores": scores,
            "status": t["status"],
        })

    # Group by variant
    variants = sorted(list({r["variant_id"] for r in records if r["variant_id"]}))
    variant_stats = {}

    for var in variants:
        v_recs = [r for r in records if r["variant_id"] == var]
        n = len(v_recs)
        tc = [r["scores"].get("task_completion", 0.0) for r in v_recs]
        cs = [r["scores"].get("claim_support", 0.0) for r in v_recs]
        vp = [r["scores"].get("valid_plan", 0.0) for r in v_recs]
        auth = [r["scores"].get("authorization", 0.0) for r in v_recs]
        ans = [r["scores"].get("answerability_abstention", 0.0) for r in v_recs]

        variant_stats[var] = {
            "n": n,
            "task_completion": statistics.mean(tc) if n else 0.0,
            "claim_support": statistics.mean(cs) if n else 0.0,
            "valid_plan": statistics.mean(vp) if n else 0.0,
            "authorization": statistics.mean(auth) if n else 0.0,
            "abstention": statistics.mean(ans) if n else 0.0,
            "records": v_recs,
        }

    print("\n--- 1. OVERALL METRICS PER VARIANT ---")
    print(f"{'Variant':25s} | {'N':4s} | {'Task Complete':13s} | {'Claim Support':13s} | {'Valid Plan':11s} | {'Authorized':11s}")
    print("-" * 88)
    for var, s in variant_stats.items():
        print(f"{var:25s} | {s['n']:4d} | {s['task_completion']*100:11.2f}% | {s['claim_support']*100:11.2f}% | {s['valid_plan']*100:9.2f}% | {s['authorization']*100:9.2f}%")

    # Repeat consistency / Uncertainty
    print("\n--- 2. REPEAT CONSISTENCY & UNCERTAINTY (across 3 repeats) ---")
    for var in variants:
        v_recs = [r for r in records if r["variant_id"] == var]
        by_conv = defaultdict(dict)
        for r in v_recs:
            by_conv[r["conversation_id"]][r["repetition"]] = r["scores"].get("task_completion", 0.0)
        
        multi_repeats = [list(reps.values()) for reps in by_conv.values() if len(reps) > 1]
        if multi_repeats:
            stds = [statistics.pstdev(vals) for vals in multi_repeats]
            all_agree = sum(1 for vals in multi_repeats if len(set(vals)) == 1)
            print(f"{var:25s} | Cases with multi-repeats: {len(multi_repeats):2d} | Perfect Repeat Agreement: {all_agree}/{len(multi_repeats)} ({all_agree/len(multi_repeats)*100:.1f}%) | Mean StdDev: {statistics.mean(stds):.3f}")

    # 3. Failure Breakdown
    print("\n--- 3. FAILURE REASON BREAKDOWN (where Task Completion == 0) ---")
    for var in variants:
        v_recs = [r for r in records if r["variant_id"] == var and r["scores"].get("task_completion", 0.0) == 0.0]
        n_fail = len(v_recs)
        if not n_fail:
            continue
        no_claim = sum(1 for r in v_recs if r["scores"].get("claim_support", 0.0) == 0.0)
        bad_plan = sum(1 for r in v_recs if r["scores"].get("valid_plan", 0.0) == 0.0)
        unauth = sum(1 for r in v_recs if r["scores"].get("authorization", 0.0) == 0.0)
        bad_ans = sum(1 for r in v_recs if r["scores"].get("answerability_abstention", 0.0) == 0.0)
        print(f"{var:25s} | Fails: {n_fail:2d} | Lack Claim Support: {no_claim:2d} ({no_claim/n_fail*100:.1f}%) | Invalid Plan: {bad_plan:2d} ({bad_plan/n_fail*100:.1f}%) | Unauth: {unauth:2d} ({unauth/n_fail*100:.1f}%) | Bad Abstention: {bad_ans:2d} ({bad_ans/n_fail*100:.1f}%)")

    return variant_stats

if __name__ == "__main__":
    run_analysis()
