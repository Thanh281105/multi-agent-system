import os
import sys
from pathlib import Path
from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "evaluation-v3" / "pilot-mini-luna-judge-20261014"
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

from app.evaluation import v3_cli
import json
import hashlib
from app.evaluation.benchmark_v3 import SuccessorBindingV3, canonical_sha256
from app.evaluation.v3_gold import load_evaluation_gold_v3

pilot_args = [
    "pilot",
    "--project-root", str(ROOT),
    "--experiment", "evaluation/v3/mini-generation-luna-judge-v1/experiment.v3.json",
    "--gold", "evaluation/v3/review-successor-v1/gold.v3.json",
    "--split", "evaluation/v3/review-successor-v1/split.v3.json",
    "--runtime-manifest", "output/review-remediation/live-runtime-manifest.v2.json",
    "--run-id", "run_p7_mini_luna_judge_20261014",
    "--run-key", "package7-pilot-mini-luna-judge-20261014",
    "--output", str(OUTPUT_DIR),
    "--allow-network",
    "--database-url", DATABASE_URL,
]

print("Starting P7 Pilot Live...")
code = v3_cli.cli(pilot_args)
if code != 0:
    print(f"P7 Pilot failed with code {code}")
    sys.exit(code)

print("P7 Pilot Complete! Now freezing repeat decision...")
freeze_args = [
    "repeat-freeze",
    "--project-root", str(ROOT),
    "--experiment", "evaluation/v3/mini-generation-luna-judge-v1/experiment.v3.json",
    "--gold", "evaluation/v3/review-successor-v1/gold.v3.json",
    "--split", "evaluation/v3/review-successor-v1/split.v3.json",
    "--runtime-manifest", "output/review-remediation/live-runtime-manifest.v2.json",
    "--run-id", "run_p7_mini_luna_judge_20261014",
    "--run-key", "package7-pilot-mini-luna-judge-20261014",
    "--output", str(OUTPUT_DIR),
]

freeze_code = v3_cli.cli(freeze_args)
if freeze_code != 0:
    print(f"Repeat freeze failed with code {freeze_code}")
    sys.exit(freeze_code)

print("Generating successor binding for P8...")
protocol_bytes = (OUTPUT_DIR / "protocol.v3.json").read_bytes()
protocol_sha256 = hashlib.sha256(protocol_bytes).hexdigest()

repeat_bytes = (OUTPUT_DIR / "repeat-decision.v3.json").read_bytes()
repeat_decision_sha256 = hashlib.sha256(repeat_bytes).hexdigest()

gold_path = "evaluation/v3/review-successor-v1/gold.v3.json"
split_path = "evaluation/v3/review-successor-v1/split.v3.json"
loaded = load_evaluation_gold_v3(
    ROOT / gold_path,
    ROOT / split_path,
    project_root=ROOT,
)

manifest_path = "output/review-remediation/live-runtime-manifest.v2.json"
manifest_sha256 = hashlib.sha256((ROOT / manifest_path).read_bytes()).hexdigest()

data = {
    "schema_version": "3.1",
    "binding_id": "mini_generation_luna_judge_p8_successor_v1",
    "evaluator_contract": "evidence_semantics_v2",
    "protocol_sha256": protocol_sha256,
    "repeat_decision_sha256": repeat_decision_sha256,
    "gold_sha256": loaded.gold_sha256,
    "split_sha256": loaded.split_sha256,
    "experiment_path": "evaluation/v3/mini-generation-luna-judge-v1/experiment.v3.json",
    "gold_path": gold_path,
    "split_path": split_path,
    "runtime_manifest_path": manifest_path,
    "runtime_manifest_sha256": manifest_sha256,
}
binding_sha256 = canonical_sha256(data)
data["binding_sha256"] = binding_sha256

binding = SuccessorBindingV3.model_validate(data)
out_file = OUTPUT_DIR / "successor-binding.v3.json"
out_file.write_text(json.dumps(binding.model_dump(mode="json"), indent=2), encoding="utf-8")
print(f"SUCCESS: P7 Pilot, repeat freeze, and successor binding completed cleanly in {OUTPUT_DIR}!")
