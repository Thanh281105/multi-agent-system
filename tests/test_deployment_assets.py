import tomllib
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.db.migrate import _migration_root
from scripts import ci_live_smoke

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
        "ecommerce-evaluate-v3": "app.evaluation.v3_cli:main",
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
    assert ci_live_smoke._readiness_checks_pass(
        {"runtime": "ok", "database": "ok", "knowledge": "disabled"}
    )
    assert not ci_live_smoke._readiness_checks_pass(
        {"runtime": "ok", "database": "disabled", "knowledge": "disabled"}
    )
    assert not ci_live_smoke._readiness_checks_pass(
        {"runtime": "ok", "database": "ok", "knowledge": "failed"}
    )


def test_live_smoke_reads_v2_identity_and_history_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []
    gateway_key = "smoke-gateway-secret"
    monkeypatch.setattr(ci_live_smoke, "GATEWAY_KEY", gateway_key)

    def fake_request(
        method: str, path: str, **kwargs: object
    ) -> ci_live_smoke.HttpResult:
        requests.append((method, path))
        assert kwargs["headers"] == {"X-API-Key": gateway_key}
        if path == "/api/v2/me":
            return ci_live_smoke.HttpResult(
                200,
                b'{"principal_id":"smoke-user","allowed_modes":["shopper"]}',
            )
        if path == "/api/v2/conversations?mode=shopper":
            return ci_live_smoke.HttpResult(200, b'{"conversations":[]}')
        pytest.fail(f"unexpected live smoke request: {method} {path}")

    monkeypatch.setattr(ci_live_smoke, "_request", fake_request)

    ci_live_smoke._assert_v2_identity_and_history()

    assert requests == [
        ("GET", "/api/v2/me"),
        ("GET", "/api/v2/conversations?mode=shopper"),
    ]


def test_live_smoke_accepts_unpublished_v2_history_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_key = "smoke-gateway-secret"
    monkeypatch.setattr(ci_live_smoke, "GATEWAY_KEY", gateway_key)

    def fake_request(
        method: str, path: str, **kwargs: object
    ) -> ci_live_smoke.HttpResult:
        assert kwargs["headers"] == {"X-API-Key": gateway_key}
        if path == "/api/v2/me":
            return ci_live_smoke.HttpResult(
                200,
                b'{"principal_id":"smoke-user","allowed_modes":["shopper"]}',
            )
        if path == "/api/v2/conversations?mode=shopper":
            return ci_live_smoke.HttpResult(
                503,
                b'{"error":{"code":"v2.runtime_unavailable"}}',
            )
        pytest.fail(f"unexpected live smoke request: {method} {path}")

    monkeypatch.setattr(ci_live_smoke, "_request", fake_request)

    ci_live_smoke._assert_v2_identity_and_history()


def test_live_smoke_requires_retired_v1_route_to_return_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    def fake_request(
        method: str, path: str, **kwargs: object
    ) -> ci_live_smoke.HttpResult:
        requests.append((method, path))
        assert kwargs["headers"]["X-API-Key"] == ci_live_smoke.GATEWAY_KEY
        return ci_live_smoke.HttpResult(404, b'{"detail":"Not Found"}')

    monkeypatch.setattr(ci_live_smoke, "_request", fake_request)

    ci_live_smoke._assert_v1_routes_removed()

    assert requests == [("POST", "/api/v1/chat")]
