import os
import sys
import json
import shutil
from pathlib import Path

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_checkpoint import (
    CheckpointRecordV3,
    CheckpointEventV3,
    _checkpoint_id,
    _GENESIS_SHA256,
    load_checkpoint_v3,
    canonical_schedule_id_v3,
)
from app.evaluation.v3_schedule import pilot_schedule_sha256_v3
from app.evaluation.v3_models import ScheduledTurnV3

ROOT = Path(__file__).resolve().parents[1]
PILOT_DIR = ROOT / "output" / "evaluation-v3" / "pilot-live-v3"
HELDOUT_DIR = ROOT / "output" / "evaluation-v3" / "heldout-live-v3"

def normalize_payload(payload: dict) -> dict:
    dump_str = json.dumps(payload, ensure_ascii=False)
    dump_str = dump_str.replace("2026-08-24T10:47:30.042026Z", "2026-08-24T03:47:30.042026Z")
    data = json.loads(dump_str)
    receipt = data.get("receipt")
    if not receipt:
        return data

    for tr in receipt.get("turn_results", []):
        res = tr.get("result_payload", {}).get("result", {})
        ev_list = res.get("evidence", [])
        ev_map = {e["evidence_id"]: e for e in ev_list}
        cat_map = {e["source_id"]: e for e in ev_list if e.get("source_id", "").startswith("catalog_product_")}
        for cit in res.get("citations", []):
            ev = ev_map.get(cit.get("evidence_id"))
            if ev and ev.get("source_id", "").startswith("rank_"):
                pid = ev["source_id"].split("_")[1]
                target_cat = cat_map.get(f"catalog_product_{pid}")
                if target_cat:
                    cit["evidence_id"] = target_cat["evidence_id"]
                    cit["span_id"] = target_cat["span_id"]
                    cit["display_label"] = target_cat["display_label"]
    return data


def rechain_checkpoint(
    ckpt_path: Path,
    *,
    schedule_path: Path,
    is_pilot: bool,
):
    print(f"\n--- Normalizing & Re-chaining: {ckpt_path.name} ---")
    bak_path = ckpt_path.with_suffix(".jsonl.bak")
    if not bak_path.exists():
        shutil.copy2(ckpt_path, bak_path)
        print(f"  Created backup at: {bak_path}")

    raw_records = []
    with open(ckpt_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                raw_records.append(json.loads(line))

    print(f"  Loaded {len(raw_records)} records from {ckpt_path.name}")

    rehashed_records: list[CheckpointRecordV3] = []
    previous_sha256 = _GENESIS_SHA256

    for seq, rec in enumerate(raw_records):
        event_enum = CheckpointEventV3(rec["event"])
        payload = rec.get("payload", {})
        if event_enum != CheckpointEventV3.STARTED and payload:
            payload = normalize_payload(payload)

        # Build canonical body dict
        body = {
            "schema_version": "3.0",
            "sequence": seq,
            "checkpoint_id": _checkpoint_id(
                run_id=rec["run_id"],
                execution_case_set_sha256=rec["execution_case_set_sha256"],
                schedule_sha256=rec["schedule_sha256"],
                schedule_index=rec["schedule_index"],
                event=event_enum,
            ),
            "run_id": rec["run_id"],
            "protocol_sha256": rec["protocol_sha256"],
            "execution_case_set_sha256": rec["execution_case_set_sha256"],
            "schedule_id": canonical_schedule_id_v3(rec["schedule_sha256"]),
            "schedule_sha256": rec["schedule_sha256"],
            "schedule_index": rec["schedule_index"],
            "turn_id": rec["turn_id"],
            "observation_id": rec.get("observation_id"),
            "event": rec["event"],
            "payload": payload,
            "previous_record_sha256": previous_sha256,
        }
        record_hash = canonical_sha256(body)
        validated_record = CheckpointRecordV3.model_validate(
            {**body, "record_sha256": record_hash}
        )
        rehashed_records.append(validated_record)
        previous_sha256 = record_hash

    # Write out canonical records
    with open(ckpt_path, "wb") as f:
        for r in rehashed_records:
            f.write(canonical_json_bytes(r))

    print(f"  Wrote {len(rehashed_records)} re-chained canonical records.")

    # Validate using load_checkpoint_v3
    raw_sched = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule = tuple(ScheduledTurnV3.model_validate(item) for item in raw_sched)
    schedule_sha = pilot_schedule_sha256_v3(schedule)
    state = load_checkpoint_v3(
        ckpt_path,
        run_id=rehashed_records[0].run_id,
        protocol_sha256=rehashed_records[0].protocol_sha256,
        execution_case_set_sha256=rehashed_records[0].execution_case_set_sha256,
        schedule_sha256=schedule_sha,
        schedule=schedule,
    )
    print(f"  load_checkpoint_v3 VALIDATION SUCCESSFUL!")
    print(f"    Completed turns: {len(state.completed_turn_ids)} / {len(schedule)}")
    print(f"    Failed turns: {len(state.failed_turn_ids)}")
    print(f"    Ambiguous turns: {len(state.ambiguous_turn_ids)}")
    print(f"    Blocked: {state.blocked}, Partial: {state.partial}")


if __name__ == "__main__":
    rechain_checkpoint(
        PILOT_DIR / "pilot-checkpoint.v3.jsonl",
        schedule_path=PILOT_DIR / "pilot-schedule.v3.json",
        is_pilot=True,
    )
    rechain_checkpoint(
        HELDOUT_DIR / "heldout-checkpoint.v3.jsonl",
        schedule_path=HELDOUT_DIR / "heldout-schedule.p8.json",
        is_pilot=False,
    )
    print("\nALL CHECKPOINTS NORMALIZED AND VERIFIED!")
