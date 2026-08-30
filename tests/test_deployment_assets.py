import tomllib
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.db.migrate import _migration_root
from app.knowledge import seed as knowledge_seed
from app.knowledge.qdrant import KnowledgeStoreUnavailableError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_container_runs_as_non_root_with_healthcheck() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("FROM ") == 3
    assert "FROM node:24.19.0-bookworm-slim AS frontend-builder" in dockerfile
    assert "COPY frontend/package.json frontend/package-lock.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert (
        "COPY --from=frontend-builder /build/app/frontend/dist ./app/frontend/dist"
    ) in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "python:3.12.14-slim-bookworm" in dockerfile
    assert (
        "COPY --chown=10001:10001 data/snapshots/tiki-books-v4-eval "
        "./data/snapshots/tiki-books-v4-eval"
    ) in dockerfile


def test_container_context_excludes_secrets_dependencies_and_generated_assets() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for ignored in (".env", ".env.*", "frontend/node_modules", "dist", "output"):
        assert ignored in dockerignore.splitlines()
    assert "data/**" in dockerignore.splitlines()
    assert "!data/snapshots/tiki-books-v4-eval/**" in dockerignore.splitlines()
    assert "!data/snapshots/tiki-books-v4-test/**" not in dockerignore.splitlines()


def test_runtime_snapshot_contains_every_quality_gated_artifact() -> None:
    snapshot = PROJECT_ROOT / "data" / "snapshots" / "tiki-books-v4-eval"

    assert {path.name for path in snapshot.iterdir() if path.is_file()} == {
        "manifest.json",
        "products.jsonl",
        "quality-report.json",
        "reviews.jsonl",
    }


def test_compose_separates_bootstrap_jobs_and_private_data_services() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(compose)

    for service in (
        "  postgres:",
        "  redis:",
        "  qdrant:",
        "  migrate:",
        "  seed-data:",
        "  seed-knowledge:",
        "  backend:",
    ):
        assert service in compose
    assert "condition: service_completed_successfully" in compose
    assert "internal: true" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert compose.count("mem_limit:") == 4
    assert compose.count("cpus:") == 4
    assert compose.count("pids_limit:") == 4
    assert "5432:5432" not in compose
    assert "6379:6379" not in compose
    assert "6333:6333" not in compose
    assert parsed["x-app-environment"]["PUBLIC_SNAPSHOT_DIR"] == (
        "/app/data/snapshots/tiki-books-v4-eval"
    )
    assert parsed["services"]["seed-data"]["depends_on"] == {
        "migrate": {"condition": "service_completed_successfully"}
    }
    assert parsed["services"]["backend"]["depends_on"]["seed-data"] == {
        "condition": "service_completed_successfully"
    }


def test_package_exposes_operational_entry_points() -> None:
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert metadata["project"]["version"] == "1.0.0"
    assert metadata["project"]["scripts"] == {
        "ecommerce-data": "app.data.cli:main",
        "ecommerce-evaluate": "app.evaluation.runner:main",
        "ecommerce-evaluate-v2": "app.evaluation.v2_runner:main",
        "ecommerce-migrate": "app.db.migrate:main",
        "ecommerce-reset-legacy-seed": "app.db.reset_legacy_seed:main",
        "ecommerce-seed": "app.db.seed:main",
        "ecommerce-seed-knowledge": "app.knowledge.seed:main",
    }


def test_snapshot_directory_setting_has_a_deployable_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PUBLIC_SNAPSHOT_DIR", raising=False)
    assert Settings(_env_file=None).public_snapshot_dir == Path(
        "data/snapshots/tiki-books-v4-eval"
    )

    monkeypatch.setenv("PUBLIC_SNAPSHOT_DIR", str(tmp_path))
    assert Settings(_env_file=None).public_snapshot_dir == tmp_path


def test_migration_root_is_discovered_from_runtime_assets() -> None:
    assert _migration_root() == PROJECT_ROOT


def test_knowledge_seed_retries_only_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def transient_seed() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KnowledgeStoreUnavailableError("starting")
        return 7

    monotonic_values = iter((10.0, 10.0))
    monkeypatch.setattr(knowledge_seed, "seed_sample_knowledge", transient_seed)
    monkeypatch.setattr(
        knowledge_seed.time,
        "monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(knowledge_seed.time, "sleep", lambda _seconds: None)

    assert knowledge_seed.wait_and_seed_sample_knowledge(wait_seconds=5) == 7
    assert attempts == 2


@pytest.mark.parametrize("wait_seconds", [-0.1, 301])
def test_knowledge_seed_rejects_unbounded_waits(wait_seconds: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 300"):
        knowledge_seed.wait_and_seed_sample_knowledge(wait_seconds=wait_seconds)
