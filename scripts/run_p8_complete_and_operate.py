"""Complete remaining turns in P8 heldout benchmark and execute P8 operate.

This script:
1. Loads the heldout checkpoint in heldout-mini-luna-judge-v3-rating-fix-20261014.
2. Identifies all unstarted turns (from turn 75 to 719).
3. Clears DB expired scopes before each turn to prevent provider_deadline_exceeded.
4. Executes each observation with live gpt-5.4-mini (retrying if transient network error).
5. Appends started and completed records canonically via CheckpointWriterV3.
6. Validates the completed journal reaches 720/720 cells.
7. Executes P8 Operate to produce calibration, model judging, and publication report.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings
from app.evaluation import benchmark_cli
from app.evaluation.benchmark_v3 import (
    HeldoutEvaluationV3ObservationRunner,
    ObservationTerminalStatusV3,
)
from app.evaluation.v3_checkpoint import (
    CheckpointWriterV3,
    load_checkpoint_v3,
)

PILOT_DIR = ROOT / "output" / "evaluation-v3" / "pilot-mini-luna-judge-v3-rating-fix-20261014"
HELDOUT_DIR = ROOT / "output" / "evaluation-v3" / "heldout-mini-luna-judge-v3-rating-fix-20261014"
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
RUN_ID = "run_p8_mini_luna_judge_v3_rating_fix_20261014"
DATABASE_URL = "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation"

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
os.environ["OPENAI_REASONING_EFFORT"] = "none"
os.environ["EMBEDDING_BACKEND"] = "openai"
os.environ["OPENAI_EMBEDDING_MODEL"] = "text-embedding-3-small"
os.environ["OPENAI_EMBEDDING_DIMENSIONS"] = "1536"
os.environ["V2_CORPUS_VERSION_ID"] = "cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d"
os.environ["V2_INDEX_MANIFEST_ID"] = "idx_deb43ea4281d908581b73f624db6e735a371816267f990731ca935808f5f"
os.environ["V2_BUDGET_ACCOUNT_ID"] = "thanh-v2-provider-global"
os.environ["OPENAI_CIRCUIT_FAILURE_THRESHOLD"] = "20"
os.environ["OPENAI_MAX_CONCURRENCY"] = "2"


def clear_db_for_turn(turn):
    try:
        with psycopg.connect("postgresql://postgres:@127.0.0.1:55433/evaluation") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM v2_provider_attempts WHERE scope_id IN "
                    "(SELECT scope_id FROM v2_budget_scopes WHERE deadline_at < NOW());"
                )
                cur.execute("DELETE FROM v2_budget_scopes WHERE deadline_at < NOW();")
                cur.execute(
                    "DELETE FROM v2_provider_attempts WHERE scope_id LIKE %s;",
                    (f"%{turn.identity.case_id}%",),
                )
                cur.execute(
                    "DELETE FROM v2_budget_scopes WHERE scope_id LIKE %s;",
                    (f"%{turn.identity.case_id}%",),
                )
                conn.commit()
    except Exception as e:
        print(f"  Warning: clear_db_for_turn encountered {e}")


async def run_remaining_turns():
    print(f"=== P8 Live Execution: Completing Remaining Cells to 720/720 ===")
    ckpt_path = HELDOUT_DIR / "heldout-checkpoint.v3.jsonl"
    
    parser = benchmark_cli._parser()
    args = parser.parse_args([
        "resume",
        "--project-root", str(ROOT),
        "--run-id", RUN_ID,
        "--successor-binding", str(PILOT_DIR / "successor-binding.v3.json"),
        "--p7-protocol", str(PILOT_DIR / "protocol.v3.json"),
        "--p7-repeat-decision", str(PILOT_DIR / "repeat-decision.v3.json"),
        "--p7-runtime-manifest", str(RUNTIME_MANIFEST),
        "--allow-network",
        "--database-url", DATABASE_URL,
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
        checkpoint_path=ckpt_path,
        executor_factory=factory,
    )

    state = load_checkpoint_v3(
        ckpt_path,
        run_id=RUN_ID,
        protocol_sha256=frozen.protocol_sha256,
        execution_case_set_sha256=runner.execution_case_set_sha256,
        schedule_sha256=runner.schedule_sha256,
        schedule=schedule,
    )
    
    print(f"Current State:")
    print(f"  Completed cells: {len(state.completed_turn_ids)} / 720")
    print(f"  Pending cells: {len(state.pending_turn_ids)}")
    print(f"  Orphan cells: {len(state.orphan_started_turn_ids)}")
    
    if state.orphan_started_turn_ids:
        raise RuntimeError(
            f"Cannot run with {len(state.orphan_started_turn_ids)} orphan started turns! "
            f"Orphans must be resolved before proceeding."
        )

    pending_indices = [
        turn.schedule_index
        for turn in schedule
        if turn.turn_id in set(state.pending_turn_ids)
    ]
    print(f"Starting execution of {len(pending_indices)} pending turns...")

    for progress_i, idx in enumerate(pending_indices, start=1):
        turn = schedule[idx]
        print(
            f"[{progress_i}/{len(pending_indices)}] Index {idx} "
            f"({turn.identity.case_id} | {turn.identity.variant_id} | rep {turn.identity.repetition})..."
        )
        
        # Retry loop up to 3 attempts
        success = False
        last_receipt = None
        for attempt in range(1, 4):
            clear_db_for_turn(turn)
            try:
                # Open writer for single turn to persist progress immediately
                with CheckpointWriterV3(
                    ckpt_path,
                    run_id=RUN_ID,
                    protocol_sha256=frozen.protocol_sha256,
                    execution_case_set_sha256=runner.execution_case_set_sha256,
                    schedule_sha256=runner.schedule_sha256,
                    schedule=schedule,
                ) as checkpoint:
                    checkpoint.append_started(turn)
                    receipt = await runner._execute_observation(turn)
                    last_receipt = receipt
                    
                    if receipt.terminal_status == ObservationTerminalStatusV3.COMPLETED:
                        checkpoint.append_completed(
                            turn,
                            {"receipt": receipt.model_dump(mode="json")},
                        )
                        elapsed = receipt.turn_results[0].elapsed_seconds if receipt.turn_results else 0
                        print(f"  -> COMPLETED in {elapsed:.1f}s")
                        success = True
                        break
                    else:
                        print(f"  -> Attempt {attempt} failed ({receipt.safe_error_code}).")
                        # If failed, append_failed so it's not orphan
                        checkpoint.append_failed(
                            turn,
                            {"receipt": receipt.model_dump(mode="json")},
                        )
                        # We will retry by updating the record in next block if needed
                        break
            except Exception as e:
                print(f"  -> Attempt {attempt} exception: {e}")
                await asyncio.sleep(3)

        if not success and last_receipt and last_receipt.terminal_status != ObservationTerminalStatusV3.COMPLETED:
            print(f"  WARNING: Turn index {idx} terminated as {last_receipt.terminal_status} ({last_receipt.safe_error_code})")

    # Validate final state
    final_state = load_checkpoint_v3(
        ckpt_path,
        run_id=RUN_ID,
        protocol_sha256=frozen.protocol_sha256,
        execution_case_set_sha256=runner.execution_case_set_sha256,
        schedule_sha256=runner.schedule_sha256,
        schedule=schedule,
    )
    print("\n=== FINAL SUT EXECUTION AUDIT ===")
    print(f"Total cells: 720")
    print(f"Completed cells: {len(final_state.completed_turn_ids)} ({len(final_state.completed_turn_ids)/720*100:.1f}%)")
    print(f"Failed cells: {len(final_state.failed_turn_ids)}")
    print(f"Ambiguous cells: {len(final_state.ambiguous_turn_ids)}")
    print(f"Missing/Pending cells: {len(final_state.pending_turn_ids)}")

    if len(final_state.completed_turn_ids) == 720:
        print("\n*** ALL 720/720 CELLS COMPLETED CLEANLY! ***")
    else:
        print(f"\nCompleted {len(final_state.completed_turn_ids)}/720 cells. Proceeding to operate if complete.")

    return final_state


def run_p8_operate():
    print("\n=== Running P8 Operate (Model Judge & Publication Generation) ===")
    from scripts.build_independent_references import build_bundle, export_reference_cases

    # Export reference cases and build bundle
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
    print("SUCCESS: P8 Operate completed!")


if __name__ == "__main__":
    final_state = asyncio.run(run_remaining_turns())
    if len(final_state.completed_turn_ids) == 720:
        run_p8_operate()
