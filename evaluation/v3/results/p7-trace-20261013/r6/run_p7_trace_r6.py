import hashlib
import json
import os
import sys
from pathlib import Path

from app.core.config import Settings
from app.evaluation import v3_cli
from app.evaluation.benchmark_v3 import SuccessorBindingV3, canonical_sha256
from app.evaluation.v3_gold import load_evaluation_gold_v3

ROOT = Path.cwd()
OUTPUT_DIR = ROOT / "output" / "evaluation-v3" / "p7-trace-20261013-r6"
EXPERIMENT = ROOT / "evaluation" / "v3" / "review-successor-v1" / "experiment.v3.json"
GOLD = ROOT / "evaluation" / "v3" / "review-successor-v1" / "gold.v3.json"
SPLIT = ROOT / "evaluation" / "v3" / "review-successor-v1" / "split.v3.json"
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
DATABASE_URL = "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation"
RUN_ID = "run_p7_trace_20261013_r6"
RUN_KEY = "package7-pilot-trace-20261013-r6"

api_key = os.getenv("OPENAI_API_KEY") or Settings().openai_api_key_value
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not configured")
os.environ.update({
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
})

common = [
    "--project-root", str(ROOT),
    "--experiment", str(EXPERIMENT),
    "--gold", str(GOLD),
    "--split", str(SPLIT),
    "--runtime-manifest", str(RUNTIME_MANIFEST),
    "--run-id", RUN_ID,
]
pilot_args = [
    "pilot", *common,
    "--run-key", RUN_KEY,
    "--output", str(OUTPUT_DIR),
    "--allow-network",
    "--database-url", DATABASE_URL,
]
code = v3_cli.cli(pilot_args)
if code != 0:
    sys.exit(code)

freeze_args = [
    "repeat-freeze", *common,
    "--run-key", RUN_KEY,
    "--output", str(OUTPUT_DIR),
]
code = v3_cli.cli(freeze_args)
if code != 0:
    sys.exit(code)

protocol_bytes = (OUTPUT_DIR / "protocol.v3.json").read_bytes()
repeat_bytes = (OUTPUT_DIR / "repeat-decision.v3.json").read_bytes()
loaded = load_evaluation_gold_v3(GOLD, SPLIT, project_root=ROOT)
data = {
    "schema_version": "3.1",
    "binding_id": "review_p8_successor_trace_20261013_r6",
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
data["binding_sha256"] = canonical_sha256(data)
binding = SuccessorBindingV3.model_validate(data)
out_file = OUTPUT_DIR / "successor-binding.v3.json"
out_file.write_text(
    json.dumps(binding.model_dump(mode="json"), indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps({
    "status": "p7_repeat_frozen_and_successor_binding_created",
    "run_id": RUN_ID,
    "protocol_sha256": binding.protocol_sha256,
    "sut_git_revision": json.loads(protocol_bytes).get("assets", {}).get("sut_git_revision"),
    "binding_sha256": binding.binding_sha256,
    "output": str(OUTPUT_DIR.resolve()),
}))
