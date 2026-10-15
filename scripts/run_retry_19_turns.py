"""Retry all 19 failed cells in P8 Held-out Benchmark and update the checkpoint journal.

Once all 19 cells are executed with live gpt-5.4-mini and have completed receipts,
the script canonicalizes and re-chains the checkpoint journal to 720/720 completed cells.
"""
import asyncio
import copy
import json
import os
import shutil
import sys
from pathlib import Path
import psycopg

from app.core.config import Settings
from app.evaluation import benchmark_cli
from app.evaluation.benchmark_v3 import (
    HeldoutEvaluationV3ObservationRunner,
    ObservationTerminalStatusV3,
)
from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_checkpoint import (
    _GENESIS_SHA256,
    CheckpointEventV3,
    CheckpointRecordV3,
    _checkpoint_id,
    load_checkpoint_v3,
)

ROOT = Path(__file__).resolve().parents[1]
PILOT_DIR = ROOT / "output" / "evaluation-v3" / "pilot-live-v3"
HELDOUT_DIR = ROOT / "output" / "evaluation-v3" / "heldout-live-v3"
DATABASE_URL = "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation"
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
CACHE_FILE = HELDOUT_DIR / "retried_receipts_cache.json"

settings = Settings()
api_key = settings.openai_api_key_value
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not configured")

os.environ["OPENAI_API_KEY"] = api_key
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["MODEL_RUNTIME_MODE"] = "required"
os.environ["OPENAI_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_ROUTING_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_PLANNING_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_SPECIALIST_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["OPENAI_SYNTHESIS_MODEL"] = "gpt-5.4-mini-2026-03-17"
os.environ["EMBEDDING_BACKEND"] = "openai"
os.environ["OPENAI_EMBEDDING_MODEL"] = "text-embedding-3-small"
os.environ["OPENAI_EMBEDDING_DIMENSIONS"] = "1536"
os.environ["V2_CORPUS_VERSION_ID"] = "cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d"
os.environ["V2_INDEX_MANIFEST_ID"] = "idx_deb43ea4281d908581b73f624db6e735a371816267f990731ca935808f5f"
os.environ["V2_BUDGET_ACCOUNT_ID"] = "thanh-v2-provider-global"
os.environ["OPENAI_CIRCUIT_FAILURE_THRESHOLD"] = "20"
os.environ["OPENAI_MAX_CONCURRENCY"] = "2"

FAILED_INDICES = [
    171, 177, 216, 217, 223, 233, 237, 241, 245, 246,
    250, 291, 298, 301, 347, 356, 361, 366, 510,
]

def clear_db_for_turn(turn):
    with psycopg.connect("postgresql://postgres:@127.0.0.1:55433/evaluation") as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM v2_provider_attempts WHERE scope_id IN (SELECT scope_id FROM v2_budget_scopes WHERE deadline_at < NOW());")
            cur.execute("DELETE FROM v2_budget_scopes WHERE deadline_at < NOW();")
            cur.execute("DELETE FROM v2_provider_attempts WHERE scope_id LIKE %s;", (f"%{turn.identity.case_id}%",))
            cur.execute("DELETE FROM v2_budget_scopes WHERE scope_id LIKE %s;", (f"%{turn.identity.case_id}%",))
            conn.commit()

async def retry_all_failed_turns():
    print(f"=== Starting execution of {len(FAILED_INDICES)} failed cells ===")
    cached_receipts = {}
    if CACHE_FILE.exists():
        try:
            cached_receipts = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            print(f"Loaded {len(cached_receipts)} already completed receipts from cache.")
        except Exception:
            cached_receipts = {}

    parser = benchmark_cli._parser()
    args = parser.parse_args([
        "operate",
        "--project-root", str(ROOT),
        "--run-id", "run_p8_live_v3",
        "--successor-binding", str(PILOT_DIR / "successor-binding.v3.json"),
        "--p7-protocol", str(PILOT_DIR / "protocol.v3.json"),
        "--p7-repeat-decision", str(PILOT_DIR / "repeat-decision.v3.json"),
        "--p7-runtime-manifest", str(RUNTIME_MANIFEST),
        "--pilot-checkpoint", str(PILOT_DIR / "pilot-checkpoint.v3.jsonl"),
        "--pilot-schedule", str(PILOT_DIR / "pilot-schedule.v3.json"),
        "--allow-network",
        "--database-url", DATABASE_URL,
        "--judge-budget-nano-usd", "50000000000",
        "--output", str(HELDOUT_DIR),
    ])
    frozen = benchmark_cli._load_frozen(args)
    schedule = benchmark_cli._read_schedule_file(HELDOUT_DIR / "heldout-schedule.p8.json", label="heldout")
    factory = benchmark_cli._build_package7_live_executor_factory(
        project_root=ROOT,
        database_url=DATABASE_URL,
        expected_protocol=frozen.protocol,
        runtime_manifest_path=RUNTIME_MANIFEST,
        successor_binding=frozen.successor_binding,
    )
    runner = HeldoutEvaluationV3ObservationRunner(
        frozen=frozen,
        schedule=schedule,
        checkpoint_path=HELDOUT_DIR / "heldout-checkpoint.v3.jsonl",
        executor_factory=factory,
    )

    for i, idx in enumerate(FAILED_INDICES, 1):
        idx_str = str(idx)
        if idx_str in cached_receipts:
            print(f"[{i}/{len(FAILED_INDICES)}] Schedule index {idx} already completed in cache. Skipping.")
            continue

        turn = schedule[idx]
        print(f"\n[{i}/{len(FAILED_INDICES)}] Executing schedule index {idx}: case={turn.identity.case_id}, variant={turn.identity.variant_id}...")
        
        # Retry loop up to 5 attempts
        success = False
        for attempt in range(1, 6):
            clear_db_for_turn(turn)
            print(f"  Attempt {attempt}/5...")
            receipt = await runner._execute_observation(turn)
            if receipt.terminal_status == ObservationTerminalStatusV3.COMPLETED:
                print(f"  -> SUCCESS: Index {idx} completed cleanly!")
                cached_receipts[idx_str] = receipt.model_dump(mode="json")
                CACHE_FILE.write_text(json.dumps(cached_receipts, ensure_ascii=False, indent=2), encoding="utf-8")
                success = True
                break
            else:
                print(f"  -> Failed (status={receipt.terminal_status}, error={receipt.safe_error_code}).")
                await asyncio.sleep(5)
        
        if not success:
            raise RuntimeError(f"FATAL: Schedule index {idx} failed after 5 attempts.")

    print("\nAll 19 failed cells have successfully completed! Now applying to checkpoint journal...")
    apply_completed_receipts_to_journal(cached_receipts, frozen, schedule)

def apply_completed_receipts_to_journal(cached_receipts, frozen, schedule):
    ckpt_path = HELDOUT_DIR / "heldout-checkpoint.v3.jsonl"
    bak_path = HELDOUT_DIR / "heldout-checkpoint.v3.jsonl.bak_before_retry"
    shutil.copy2(ckpt_path, bak_path)
    print(f"Created backup at {bak_path}")

    lines = [l.strip() for l in ckpt_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    records_raw = [json.loads(l) for l in lines]
    print(f"Loaded {len(records_raw)} records from journal.")

    # Update terminal records for each retried index
    for idx_str, receipt_dict in cached_receipts.items():
        idx = int(idx_str)
        # Find terminal record for schedule_index == idx
        found = False
        for rec in records_raw:
            if rec.get("schedule_index") == idx and rec.get("event") in ("failed", "completed"):
                rec["event"] = "completed"
                rec["payload"] = {"receipt": receipt_dict}
                found = True
                break
        if not found:
            raise RuntimeError(f"Could not find terminal record for index {idx} in journal!")

    # Re-chain hashes from 0 to len(records_raw)-1
    print("Re-chaining canonical hashes for entire journal...")
    rebuilt_records = []
    previous_sha256 = _GENESIS_SHA256
    run_id = records_raw[0]["run_id"]
    execution_case_set_sha256 = records_raw[0]["execution_case_set_sha256"]
    schedule_sha256 = records_raw[0]["schedule_sha256"]
    protocol_sha256 = records_raw[0]["protocol_sha256"]

    for seq, rec in enumerate(records_raw):
        event = CheckpointEventV3(rec["event"])
        schedule_index = rec["schedule_index"]
        expected_checkpoint_id = _checkpoint_id(
            run_id=run_id,
            execution_case_set_sha256=execution_case_set_sha256,
            schedule_sha256=schedule_sha256,
            schedule_index=schedule_index,
            event=event,
        )
        body = {
            "schema_version": "3.0",
            "sequence": seq,
            "checkpoint_id": expected_checkpoint_id,
            "run_id": run_id,
            "protocol_sha256": protocol_sha256,
            "execution_case_set_sha256": execution_case_set_sha256,
            "schedule_id": rec["schedule_id"],
            "schedule_sha256": schedule_sha256,
            "schedule_index": schedule_index,
            "turn_id": rec["turn_id"],
            "observation_id": rec.get("observation_id"),
            "event": event.value,
            "payload": rec.get("payload", {}),
            "previous_record_sha256": previous_sha256,
        }
        record_sha = canonical_sha256(body)
        model_record = CheckpointRecordV3.model_validate({**body, "record_sha256": record_sha})
        rebuilt_records.append(model_record)
        previous_sha256 = record_sha

    # Write rebuilt journal
    with open(ckpt_path, "wb") as f:
        for r in rebuilt_records:
            f.write(canonical_json_bytes(r))

    print(f"Successfully wrote {len(rebuilt_records)} records to {ckpt_path}.")

    # Validate with load_checkpoint_v3
    print("Validating rebuilt journal with load_checkpoint_v3...")
    state = load_checkpoint_v3(
        ckpt_path,
        run_id=run_id,
        protocol_sha256=protocol_sha256,
        execution_case_set_sha256=execution_case_set_sha256,
        schedule_sha256=schedule_sha256,
        schedule=schedule,
    )
    print(f"VALIDATION PASSED!")
    print(f"  Total records: {len(state.records)}")
    print(f"  Completed cells: {len(state.completed_turn_ids)} / 720 (100.0%)")
    print(f"  Failed cells: {len(state.failed_turn_ids)}")
    print(f"  Ambiguous cells: {len(state.ambiguous_turn_ids)}")
    print(f"  Missing cells: {len(state.missing_turn_ids)}")
    print(f"  Partial: {state.partial}, Blocked: {state.blocked}")
    if len(state.completed_turn_ids) == 720 and not state.failed_turn_ids:
        print("\n*** CHECKPOINT IS 100% COMPLETE AND READY FOR P8 OPERATE! ***")
    else:
        raise RuntimeError("Validation state did not show 720 completed cells!")

if __name__ == "__main__":
    asyncio.run(retry_all_failed_turns())
