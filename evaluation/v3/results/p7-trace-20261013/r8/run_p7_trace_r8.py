"""Run one fresh development-only P7 pilot against the current app/v2 source.

Run from the repository root after the DBA role and database are ready.
The output path and run ID are one-shot; create a new pair for a later run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.session import create_database_engine

OUTPUT_DIR = ROOT / "output" / "evaluation-v3" / "p7-trace-20261013-r8"
EXPERIMENT = ROOT / "evaluation" / "v3" / "review-successor-v1" / "experiment.v3.json"
GOLD = ROOT / "evaluation" / "v3" / "review-successor-v1" / "gold.v3.json"
SPLIT = ROOT / "evaluation" / "v3" / "review-successor-v1" / "split.v3.json"
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
DATABASE_URL = os.environ.get(
    "EVALUATION_DATABASE_URL",
    "postgresql+psycopg://eval_runner@127.0.0.1:55433/evaluation",
)
RUN_ID = "run_p7_trace_20261013_r8"
RUN_KEY = "package7-pilot-trace-20261013-r8"

if OUTPUT_DIR.exists():
    raise RuntimeError("P7 trace r8 output already exists; preserve it and use a new run ID")
if make_url(DATABASE_URL).username != "eval_runner":
    raise RuntimeError("P7 trace r8 requires the least-privilege eval_runner role")

engine = create_database_engine(DATABASE_URL)
try:
    with engine.connect() as connection:
        database_role = connection.exec_driver_sql("SELECT current_user").scalar_one()
finally:
    engine.dispose()
if database_role != "eval_runner":
    raise RuntimeError("P7 trace r8 connected as a role other than eval_runner")

api_key = Settings().openai_api_key_value
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not configured")

os.environ.update(
    {
        "OPENAI_API_KEY": api_key,
        "DATABASE_URL": DATABASE_URL,
        "MODEL_RUNTIME_MODE": "required",
        "OPENAI_MODEL": "gpt-5.4-mini-2026-03-17",
        "OPENAI_ROUTING_MODEL": "gpt-5.4-mini-2026-03-17",
        "OPENAI_PLANNING_MODEL": "gpt-5.4-mini-2026-03-17",
        "OPENAI_SPECIALIST_MODEL": "gpt-5.4-mini-2026-03-17",
        "OPENAI_SYNTHESIS_MODEL": "gpt-5.4-mini-2026-03-17",
        "EMBEDDING_BACKEND": "openai",
        "OPENAI_EMBEDDING_MODEL": "text-embedding-3-small",
        "OPENAI_EMBEDDING_DIMENSIONS": "1536",
        "V2_CORPUS_VERSION_ID": "cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d",
        "V2_INDEX_MANIFEST_ID": "idx_deb43ea4281d908581b73f624db6e735a371816267f990731ca935808f5f",
        "V2_BUDGET_ACCOUNT_ID": "thanh-v2-provider-global",
        "OPENAI_CIRCUIT_FAILURE_THRESHOLD": "20",
        "OPENAI_MAX_CONCURRENCY": "2",
    }
)

from app.evaluation import v3_cli
from app.evaluation.benchmark_v3 import SuccessorBindingV3, canonical_sha256
from app.evaluation.v3_gold import load_evaluation_gold_v3

common = [
    "--project-root",
    str(ROOT),
    "--experiment",
    str(EXPERIMENT),
    "--gold",
    str(GOLD),
    "--split",
    str(SPLIT),
    "--runtime-manifest",
    str(RUNTIME_MANIFEST),
    "--run-id",
    RUN_ID,
]

pilot_code = v3_cli.cli(
    [
        "pilot",
        *common,
        "--run-key",
        RUN_KEY,
        "--output",
        str(OUTPUT_DIR),
        "--allow-network",
        "--database-url",
        DATABASE_URL,
    ]
)
if pilot_code != 0:
    sys.exit(pilot_code)

freeze_code = v3_cli.cli(
    ["repeat-freeze", *common, "--run-key", RUN_KEY, "--output", str(OUTPUT_DIR)]
)
if freeze_code != 0:
    sys.exit(freeze_code)

protocol_bytes = (OUTPUT_DIR / "protocol.v3.json").read_bytes()
repeat_bytes = (OUTPUT_DIR / "repeat-decision.v3.json").read_bytes()
loaded = load_evaluation_gold_v3(GOLD, SPLIT, project_root=ROOT)
binding_data = {
    "schema_version": "3.1",
    "binding_id": "review_p8_successor_trace_20261013_r8",
    "evaluator_contract": "evidence_semantics_v2",
    "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
    "repeat_decision_sha256": hashlib.sha256(repeat_bytes).hexdigest(),
    "gold_sha256": loaded.gold_sha256,
    "split_sha256": loaded.split_sha256,
    "experiment_path": EXPERIMENT.relative_to(ROOT).as_posix(),
    "gold_path": GOLD.relative_to(ROOT).as_posix(),
    "split_path": SPLIT.relative_to(ROOT).as_posix(),
    "runtime_manifest_path": RUNTIME_MANIFEST.relative_to(ROOT).as_posix(),
    "runtime_manifest_sha256": hashlib.sha256(RUNTIME_MANIFEST.read_bytes()).hexdigest(),
}
binding_data["binding_sha256"] = canonical_sha256(binding_data)
binding = SuccessorBindingV3.model_validate(binding_data)
(OUTPUT_DIR / "successor-binding.v3.json").write_text(
    json.dumps(binding.model_dump(mode="json"), indent=2) + "\n",
    encoding="utf-8",
)

print(
    json.dumps(
        {
            "status": "p7_trace_r8_frozen",
            "run_id": RUN_ID,
            "sut_git_revision": json.loads(protocol_bytes)
            .get("assets", {})
            .get("sut_git_revision"),
            "binding_sha256": binding.binding_sha256,
            "output": str(OUTPUT_DIR.resolve()),
        }
    )
)
