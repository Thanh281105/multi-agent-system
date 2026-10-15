import os
import sys
from pathlib import Path
from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PILOT_DIR = ROOT / "output" / "evaluation-v3" / "pilot-mini-luna-judge-v2-20261014"
HELDOUT_DIR = ROOT / "output" / "evaluation-v3" / "heldout-mini-luna-judge-v2-20261014"
DATABASE_URL = "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation"
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
RUN_ID = "run_p8_mini_luna_judge_v2_20261014"

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

from app.evaluation import benchmark_cli

ckpt_file = HELDOUT_DIR / "heldout-checkpoint.v3.jsonl"
cmd = "resume" if ckpt_file.exists() else "run"

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

if __name__ == "__main__":
    print(f"Starting P8 Held-out SUT Execution ({cmd})...")
    code = benchmark_cli.cli(p8_run_args)
    if code != 0:
        print(f"P8 run exited with code {code}")
        sys.exit(code)
    print("SUCCESS: P8 Held-out SUT execution completed cleanly!")
