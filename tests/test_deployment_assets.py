import tomllib
from pathlib import Path

import pytest

from app.db.migrate import _migration_root
from app.knowledge import seed as knowledge_seed
from app.knowledge.qdrant import KnowledgeStoreUnavailableError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_container_runs_as_non_root_with_healthcheck() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("FROM ") == 2
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "python:3.12.14-slim-bookworm" in dockerfile


def test_compose_separates_bootstrap_jobs_and_private_data_services() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

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
    assert "5432:5432" not in compose
    assert "6379:6379" not in compose
    assert "6333:6333" not in compose


def test_package_exposes_operational_entry_points() -> None:
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert metadata["project"]["version"] == "1.0.0"
    assert metadata["project"]["scripts"] == {
        "ecommerce-evaluate": "app.evaluation.runner:main",
        "ecommerce-migrate": "app.db.migrate:main",
        "ecommerce-seed": "app.db.seed:main",
        "ecommerce-seed-knowledge": "app.knowledge.seed:main",
    }


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
