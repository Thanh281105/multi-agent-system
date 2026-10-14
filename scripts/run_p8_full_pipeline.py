"""End-to-End P8 Held-out Live Benchmark and Judging Pipeline.

This script:
1. Validates PostgreSQL connectivity and schema readiness.
2. Cleans any dangling interrupted turns in heldout-checkpoint.v3.jsonl so resume succeeds seamlessly.
3. Resumes and runs SUT execution on all 720 cells across all variants (sa_shared_tools_rag, ma_fixed_rag, ma_adaptive_rag, ma_adaptive_no_rag).
4. Runs P8 Operate: Model Judge calibration, automated judging across all 720 cells, and publication generation.
5. Emits an audited summary of task completion, win/loss stats, category breakdown, cost, and latency.
"""
import os
import sys
import json
import shutil
import psycopg
from pathlib import Path
from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PILOT_DIR = ROOT / "output" / "evaluation-v3" / "pilot-mini-luna-judge-v3-20261014"
HELDOUT_DIR = ROOT / "output" / "evaluation-v3" / "heldout-mini-luna-judge-v3-20261014"
DATABASE_URL = "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation"
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
RUN_ID = "run_p8_mini_luna_judge_v3_20261014"

settings = Settings()
api_key = settings.openai_api_key_value
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not configured in .env")

# Set up runtime environment
os.environ["OPENAI_API_KEY"] = api_key
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["MODEL_RUNTIME_MODE"] = "required"
os.environ["OPENAI_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_ROUTING_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_PLANNING_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_SPECIALIST_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_SYNTHESIS_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_REASONING_EFFORT"] = "none"
os.environ["EMBEDDING_BACKEND"] = "openai"
os.environ["OPENAI_EMBEDDING_MODEL"] = "text-embedding-3-small"
os.environ["OPENAI_EMBEDDING_DIMENSIONS"] = "1536"
os.environ["V2_CORPUS_VERSION_ID"] = "cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d"
os.environ["V2_INDEX_MANIFEST_ID"] = "idx_deb43ea4281d908581b73f624db6e735a371816267f990731ca935808f5f"
os.environ["V2_BUDGET_ACCOUNT_ID"] = "thanh-v2-provider-global"
os.environ["OPENAI_CIRCUIT_FAILURE_THRESHOLD"] = "20"
os.environ["OPENAI_MAX_CONCURRENCY"] = "2"

from app.evaluation import benchmark_cli


def step1_verify_db():
    print("\n[STEP 1/5] Verifying PostgreSQL connection & snapshot...")
    with psycopg.connect("postgresql://postgres:@127.0.0.1:55433/evaluation") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM products;")
            products = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM reviews;")
            reviews = cur.fetchone()[0]
            cur.execute(
                "SELECT published_at FROM v2_knowledge_corpus_versions WHERE id = %s;",
                ("cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d",),
            )
            pub = cur.fetchone()
            print(f"  OK: {products} products, {reviews} reviews, corpus published_at={pub[0] if pub else 'N/A'}")


def step2_clean_checkpoint():
    print("\n[STEP 2/5] Checking and sanitizing checkpoint journal...")
    ckpt_file = HELDOUT_DIR / "heldout-checkpoint.v3.jsonl"
    if not ckpt_file.exists():
        print("  Checkpoint file does not exist yet; fresh start.")
        return 0

    lines = [line.strip() for line in ckpt_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        print("  Checkpoint file is empty.")
        return 0

    records = [json.loads(line) for line in lines]
    # Check if last record is 'started' without 'completed'
    if records[-1].get("event") == "started":
        print(f"  Found trailing incomplete started record at sequence {records[-1].get('sequence')}. Trimming...")
        records = records[:-1]
        ckpt_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

    # Clear any stale lock files
    for lock in HELDOUT_DIR.glob("*.lock"):
        try:
            lock.unlink()
            print(f"  Removed lock file: {lock.name}")
        except Exception:
            pass

    completed = sum(1 for r in records if r.get("event") == "completed")
    print(f"  Checkpoint clean: {completed} / 720 cells completed ({completed/720*100:.1f}%)")
    return completed


def step3_run_sut():
    ckpt_file = HELDOUT_DIR / "heldout-checkpoint.v3.jsonl"
    cmd = "resume" if ckpt_file.exists() else "run"
    print(f"\n[STEP 3/5] Running / Resuming P8 Held-out SUT Execution ({cmd}) (720 cells)...")
    p8_run_args = [
        cmd,
        "--project-root", str(ROOT),
        "--run-id", RUN_ID,
        "--successor-binding", str(PILOT_DIR / "successor-binding.v3.json"),
        "--p7-protocol", str(PILOT_DIR / "protocol.v3.json"),
        "--p7-repeat-decision", str(PILOT_DIR / "repeat-decision.v3.json"),
        "--p7-runtime-manifest", str(RUNTIME_MANIFEST),
        "--allow-network",
        "--database-url", DATABASE_URL,
        "--output", str(HELDOUT_DIR),
    ]
    code = benchmark_cli.cli(p8_run_args)
    if code != 0:
        raise RuntimeError(f"P8 run failed with exit code {code}")
    print("  SUCCESS: P8 Held-out SUT execution completed!")


def step4_run_operate():
    print("\n[STEP 4/5] Running P8 Operate (Model Judge & Publication Generation)...")
    from scripts.build_independent_references import build_bundle, export_reference_cases

    export_reference_cases()
    build_bundle()
    p8_operate_args = [
        "operate",
        "--project-root", str(ROOT),
        "--run-id", RUN_ID,
        "--successor-binding", str(PILOT_DIR / "successor-binding.v3.json"),
        "--p7-protocol", str(PILOT_DIR / "protocol.v3.json"),
        "--p7-repeat-decision", str(PILOT_DIR / "repeat-decision.v3.json"),
        "--p7-runtime-manifest", str(RUNTIME_MANIFEST),
        "--pilot-checkpoint", str(PILOT_DIR / "pilot-checkpoint.v3.jsonl"),
        "--pilot-schedule", str(PILOT_DIR / "pilot-schedule.v3.json"),
        "--allow-network",
        "--database-url", DATABASE_URL,
        "--judge-budget-nano-usd", "16000000000",
        "--independent-references",
        str(HELDOUT_DIR / "independent-calibration-references.p8.json"),
        "--output", str(HELDOUT_DIR),
    ]
    code = benchmark_cli.cli(p8_operate_args)
    if code != 0:
        raise RuntimeError(f"P8 operate failed with exit code {code}")
    print("  SUCCESS: P8 Operate completed!")


def step5_summarize_results():
    print("\n[STEP 5/5] Auditing Final P8 Live Publication Results...")
    pub_dir = HELDOUT_DIR / "p8-publication"
    if not pub_dir.exists():
        sub_pubs = list(HELDOUT_DIR.glob("**/p8-publication"))
        if sub_pubs:
            pub_dir = sub_pubs[0]
        else:
            print("  Warning: p8-publication directory not found.")
            return

    summary_file = pub_dir / "p8-report" / "summary.json"
    comp_file = pub_dir / "p8-report" / "comparisons.csv"

    if summary_file.exists():
        with open(summary_file, encoding="utf-8") as f:
            summary = json.load(f)
        print(f"  Total accepted observations: {summary.get('accepted_observation_count')}")
        print(f"  Completion status: {summary.get('completion_status')}")
        print(f"  Effective cost: ${summary.get('effective_cost_usd')}")

    if comp_file.exists():
        print("\n=== COMPARISONS (sa_shared_tools_rag vs Multi-Agent variants) ===")
        import csv
        with open(comp_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("metric") in ["task_completion", "claim_support", "citation_precision", "valid_plan"]:
                    print(
                        f"  [{row.get('candidate_variant')}] {row.get('metric')}: "
                        f"delta={float(row.get('mean_delta', 0))*100:+.2f}% "
                        f"(wins={row.get('candidate_wins')}, ties={row.get('ties')}, losses={row.get('candidate_losses')})"
                    )


if __name__ == "__main__":
    step1_verify_db()
    completed = step2_clean_checkpoint()
    step3_run_sut()
    step4_run_operate()
    step5_summarize_results()
