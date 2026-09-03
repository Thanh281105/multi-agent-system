import tomllib
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.db.migrate import _migration_root
from scripts.ci_live_smoke import (
    BOOK_COMPARE_QUERY,
    BOOK_SEARCH_QUERY,
    _book_concurrency_query,
    _readiness_checks_pass,
)

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
    assert parsed["x-app-environment"]["KNOWLEDGE_BACKEND"] == (
        "${KNOWLEDGE_BACKEND:-disabled}"
    )
    assert parsed["services"]["qdrant"]["profiles"] == ["knowledge"]
    assert "seed-knowledge" not in parsed["services"]
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


def test_live_smoke_accepts_only_the_explicitly_disabled_knowledge_check() -> None:
    assert _readiness_checks_pass(
        {"runtime": "ok", "database": "ok", "knowledge": "disabled"}
    )
    assert not _readiness_checks_pass(
        {"runtime": "ok", "database": "disabled", "knowledge": "disabled"}
    )
    assert not _readiness_checks_pass(
        {"runtime": "ok", "database": "ok", "knowledge": "failed"}
    )


def test_live_smoke_uses_book_queries_backed_by_the_runtime_snapshot() -> None:
    assert BOOK_SEARCH_QUERY == "Tìm sách Nhật Ký Tarot"
    assert BOOK_COMPARE_QUERY == ("So sánh sách Nhật Ký Tarot với Ông Nội Vượt Ngục.")
    assert _book_concurrency_query(0) == (
        "Tìm sách dưới 150 nghìn, bán tốt và ít bị khách phàn nàn."
    )
    assert _book_concurrency_query(7).startswith("Tìm sách dưới 157 nghìn")
