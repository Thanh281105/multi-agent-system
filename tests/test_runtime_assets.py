import tomllib
from pathlib import Path

import pytest

from app.core.config import Settings
from app.db.migrate import _migration_root
from scripts import ci_live_smoke

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_snapshot_contains_every_quality_gated_artifact() -> None:
    snapshot = PROJECT_ROOT / "data" / "snapshots" / "tiki-books-v4-eval"

    assert {path.name for path in snapshot.iterdir() if path.is_file()} == {
        "manifest.json",
        "products.jsonl",
        "quality-report.json",
        "reviews.jsonl",
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


def test_live_smoke_accepts_http_and_readiness_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations_key = "smoke-operations-secret"
    monkeypatch.setattr(ci_live_smoke, "OPERATIONS_KEY", operations_key)

    def fake_request(
        method: str, path: str, **kwargs: object
    ) -> ci_live_smoke.HttpResult:
        assert (method, path) == ("GET", "/metrics")
        assert kwargs["headers"] == {"X-Operations-Key": operations_key}
        return ci_live_smoke.HttpResult(
            200,
            b"http_requests_total 1\ndependency_readiness_checks_total 2\n",
        )

    monkeypatch.setattr(ci_live_smoke, "_request", fake_request)

    ci_live_smoke._assert_metrics()


@pytest.mark.parametrize(
    "body",
    (
        b"http_requests_total 1\n",
        b"dependency_readiness_checks_total 2\n",
    ),
)
def test_live_smoke_rejects_missing_expected_metric(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    operations_key = "smoke-operations-secret"
    monkeypatch.setattr(ci_live_smoke, "OPERATIONS_KEY", operations_key)
    monkeypatch.setattr(
        ci_live_smoke,
        "_request",
        lambda *args, **kwargs: ci_live_smoke.HttpResult(200, body),
    )

    with pytest.raises(SystemExit, match="live smoke failed: metrics"):
        ci_live_smoke._assert_metrics()
