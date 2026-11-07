"""Bootstrap PostgreSQL database for evaluation runtime."""
import os
import sys
import subprocess
from datetime import datetime, UTC, timedelta
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings
settings = Settings()
api_key = settings.openai_api_key_value
if not api_key:
    raise RuntimeError("OPENAI_API_KEY must be set in .env")

DATABASE_URL = "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation"
sub_env = os.environ.copy()
sub_env["DATABASE_URL"] = DATABASE_URL
sub_env["OPENAI_API_KEY"] = api_key

print("1. Running alembic migrations...")
res = subprocess.run([str(ROOT / ".venv" / "Scripts" / "alembic.exe"), "upgrade", "head"], cwd=ROOT, env=sub_env)
if res.returncode != 0:
    raise RuntimeError("Alembic upgrade failed")

print("2. Importing eval dataset snapshot...")
res2 = subprocess.run([
    str(ROOT / ".venv" / "Scripts" / "python.exe"),
    "-m", "app.data.cli",
    "import",
    "--snapshot", "data/snapshots/tiki-books-v4-eval"
], cwd=ROOT, env=sub_env)
if res2.returncode != 0:
    raise RuntimeError("Data import failed")

print("3. Creating budget account and ingestion scope...")
from app.shared.budget import PricingManifest, SQLProviderBudgetLedger
engine = create_engine(DATABASE_URL)
sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
pricing = PricingManifest.load(ROOT / "app/shared/pricing-manifest.json")
ledger = SQLProviderBudgetLedger(sessions, pricing)

account_id = "thanh-v2-provider-global"
scope_id = "review-authorship-corpus-v2-20261004"

try:
    ledger.create_account(
        account_id=account_id,
        hard_limit_nano_usd=100_000_000_000,
        warning_threshold_nano_usd=50_000_000_000,
        max_concurrency=2,
    )
except Exception:
    pass

with engine.begin() as conn:
    conn.execute(text("DELETE FROM v2_budget_scopes WHERE scope_id = :sid"), {"sid": scope_id})

ledger.create_scope(
    scope_id=scope_id,
    account_id=account_id,
    purpose="ingestion",
    hard_limit_nano_usd=5_000_000_000,
    deadline_at=datetime.now(UTC) + timedelta(seconds=59),
    max_generation_calls=1,
    max_provider_attempts=16,
    max_concurrency=2,
)

print("4. Building and publishing book knowledge corpus...")
res3 = subprocess.run([
    str(ROOT / ".venv" / "Scripts" / "python.exe"),
    "-m", "scripts.build_book_corpus",
    "--database-url", DATABASE_URL,
    "--budget-account-id", account_id,
    "--budget-scope-id", scope_id,
    "--build-owner-id", scope_id,
    "--sources", str(ROOT / "data/knowledge/books-v2/sources.json"),
    "--mappings", str(ROOT / "data/knowledge/books-v2/mappings.json"),
    "--corpus-version", "books-v2-authorship-20261004",
    "--retrieval-policy", str(ROOT / "data/knowledge/books-v1/calibration/selected-policy.json"),
    "--publish",
], cwd=ROOT, env=sub_env)
if res3.returncode != 0:
    raise RuntimeError(f"Corpus ingestion failed with code {res3.returncode}")

print("5. Aligning published_at timestamp to frozen contract...")
with engine.begin() as conn:
    conn.execute(text("UPDATE v2_knowledge_corpus_versions SET published_at = '2026-10-04 14:28:49.458015+00:00' WHERE id = 'cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d'"))

print("Database bootstrap successfully completed!")
