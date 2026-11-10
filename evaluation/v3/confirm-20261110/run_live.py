"""Chạy pilot P7 cho lần xác nhận 2026-11-10 (DB dùng một lần từ compose.yaml)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFIRM = ROOT / "evaluation" / "v3" / "confirm-20261110"
OUT = ROOT / "output" / "evaluation-v3" / "confirm-20261110"
RUNTIME_MANIFEST = OUT / "runtime-manifest.json"
EXPERIMENT = (
    ROOT / "evaluation" / "v3" / "mini-generation-luna-judge-v1" / "experiment.v3.json"
)
DATABASE_URL = (  # noqa: E501
    "postgresql+psycopg://ecommerce_p1:p1-local-disposable-only@127.0.0.1:55432/ecommerce_p1"
)
MODEL = "gpt-5.4-mini-2026-03-17"
RUN_ID = "run_p7_confirm_20261110"
RUN_KEY = "package7-pilot-confirm-20261110"
CORPUS_ID = "cor_17992e80bd6be14e465994c571308e9710642342f4aebef4a8db5fc54881"
INDEX_ID = "idx_eba47dd90e51dc1808c23b1a1a6215034c1a8622d22fce2acef3df2d2148"


def runtime_env() -> dict[str, str]:
    from app.core.config import Settings

    key = Settings().openai_api_key_value
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    env = os.environ.copy()
    env.update(
        {
            "OPENAI_API_KEY": key,
            "DATABASE_URL": DATABASE_URL,
            "MODEL_RUNTIME_MODE": "required",
            "OPENAI_MODEL": MODEL,
            "OPENAI_ROUTING_MODEL": MODEL,
            "OPENAI_PLANNING_MODEL": MODEL,
            "OPENAI_SPECIALIST_MODEL": MODEL,
            "OPENAI_SYNTHESIS_MODEL": MODEL,
            "EMBEDDING_BACKEND": "openai",
            "OPENAI_EMBEDDING_MODEL": "text-embedding-3-small",
            "OPENAI_EMBEDDING_DIMENSIONS": "1536",
            "V2_CORPUS_VERSION_ID": CORPUS_ID,
            "V2_INDEX_MANIFEST_ID": INDEX_ID,
            "V2_BUDGET_ACCOUNT_ID": "thanh-v2-provider-global",
            "OPENAI_CIRCUIT_FAILURE_THRESHOLD": "20",
            "OPENAI_MAX_CONCURRENCY": "1",
        }
    )
    return env


def common(extra: list[str]) -> list[str]:
    return [
        "--gold", str(CONFIRM / "gold.v3.json"),
        "--split", str(CONFIRM / "split.v3.json"),
        "--experiment", str(EXPERIMENT),
        "--runtime-manifest", str(RUNTIME_MANIFEST),
        "--run-id", RUN_ID,
        "--run-key", RUN_KEY,
        "--output", str(OUT / "pilot"),
        *extra,
    ]  # fmt: skip


def main(stage: str) -> int:
    sys.path.insert(0, str(ROOT))
    os.environ.update(runtime_env())
    os.chdir(ROOT)
    from app.evaluation import v3_cli

    if stage == "pilot":
        return v3_cli.cli(
            [
                "pilot",
                *common(
                    ["--allow-network", "--database-url", DATABASE_URL,
                     "--run-budget-usd", "1.5"]
                ),
            ]  # fmt: skip
        )
    if stage == "resume":
        return v3_cli.cli(
            ["resume", *common(["--allow-network", "--database-url", DATABASE_URL])]
        )
    if stage == "freeze":
        return v3_cli.cli(["repeat-freeze", *common([])])
    raise SystemExit(f"unknown stage: {stage}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
