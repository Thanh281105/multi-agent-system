"""React bundle packaging, SPA isolation, and security-header tests."""

from __future__ import annotations

import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.gateway import frontend as frontend_module
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _AssetCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self,
        _tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        for name, value in attributes:
            if name in {"href", "src"} and value and value.startswith("/assets/"):
                self.urls.append(value)


def test_frontend_serves_vite_shell_with_discoverable_hashed_assets() -> None:
    response = TestClient(create_app(frontend_settings())).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert "Thương Trí — Evidence Atlas" in response.text
    assert 'lang="vi"' in response.text
    assert 'id="root"' in response.text
    assert 'src="/src/' not in response.text
    assert "<style" not in response.text
    assert "onclick=" not in response.text
    assert {Path(url).suffix for url in _asset_urls(response.text)} >= {".css", ".js"}


def test_frontend_assets_are_same_origin_immutable_and_memory_only() -> None:
    client = TestClient(create_app(frontend_settings()))
    index = client.get("/")
    asset_urls = _asset_urls(index.text)

    responses = {url: client.get(url) for url in asset_urls}
    assert all(response.status_code == 200 for response in responses.values())
    assert all(
        response.headers["cache-control"] == "public, max-age=31536000, immutable"
        for response in responses.values()
    )

    script = next(
        response for url, response in responses.items() if url.endswith(".js")
    )
    styles = next(
        response for url, response in responses.items() if url.endswith(".css")
    )
    assert "/api/v1/chat/stream" in script.text
    assert "sessionStorage" in script.text
    assert "localStorage" not in script.text
    assert "thuong-tri.api-key" not in script.text
    assert "AbortController" in script.text
    assert "prefers-reduced-motion" in styles.text


def test_frontend_serves_root_favicon_without_spa_fallback() -> None:
    client = TestClient(create_app(frontend_settings()))

    favicon = client.get("/favicon.svg")

    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert favicon.headers["cache-control"] == "public, max-age=86400"
    assert "<svg" in favicon.text
    assert client.get("/favicon.ico").status_code == 204


def test_spa_fallback_handles_only_safe_extensionless_client_routes() -> None:
    client = TestClient(create_app(frontend_settings()))
    index = client.get("/")

    deep_link = client.get("/sessions/demo-run")

    assert deep_link.status_code == 200
    assert deep_link.content == index.content
    assert deep_link.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "path",
    [
        "/missing.js",
        "/api/v1/not-real",
        "/docs/missing",
        "/%2e%2e/pyproject.toml",
        "/assets/%2e%2e/index.html",
        "/.git/config",
    ],
)
def test_spa_fallback_rejects_server_file_and_traversal_paths(path: str) -> None:
    response = TestClient(create_app(frontend_settings())).get(path)

    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("text/html")


def test_frontend_installation_fails_closed_when_bundle_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(frontend_module, "_FRONTEND_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="packaged frontend assets are missing"):
        frontend_module.install_frontend(FastAPI())


def test_frontend_and_api_receive_strict_security_headers() -> None:
    application = create_app(frontend_settings())
    client = TestClient(application)

    frontend = client.get("/")
    unauthorized_api = client.post(
        "/api/v1/chat",
        json={"message": "Tìm laptop"},
    )

    for response in (frontend, unauthorized_api):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "'unsafe-inline'" not in frontend.headers["Content-Security-Policy"]
    assert unauthorized_api.headers["cache-control"] == "no-store"


def test_production_disables_interactive_docs_and_enables_hsts() -> None:
    production = Settings(
        _env_file=None,
        app_env="production",
        database_url=(
            "postgresql+psycopg://ecommerce:strong-db-password@localhost/ecommerce"
        ),
        gateway_api_keys="production:strong-production-key",
        legacy_chat_enabled=False,
        shared_state_backend="redis",
        redis_url="redis://:strong-redis-password@localhost:6379/0",
        operations_api_key="strong-operations-key",
        model_runtime_mode="off",
        embedding_backend="hashing",
    )
    client = TestClient(create_app(production))

    root = client.get("/")

    assert root.status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert root.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_frontend_assets_are_declared_as_python_package_data() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
    package_data = metadata["tool"]["setuptools"]["package-data"]["app.frontend"]

    assert set(package_data) == {
        "dist/index.html",
        "dist/assets/*",
        "dist/*.svg",
        "dist/*.ico",
        "dist/*.webmanifest",
    }


def _asset_urls(document: str) -> tuple[str, ...]:
    collector = _AssetCollector()
    collector.feed(document)
    return tuple(collector.urls)


def frontend_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        gateway_api_keys="frontend:frontend-secret-key",
        legacy_chat_enabled=True,
    )
