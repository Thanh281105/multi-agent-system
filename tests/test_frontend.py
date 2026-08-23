"""Static client packaging, security-header, and integration smoke tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_frontend_is_served_with_accessible_product_specific_content() -> None:
    response = TestClient(create_app(frontend_settings())).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Thương Trí — E-commerce Multi-Agent" in response.text
    assert 'lang="vi"' in response.text
    assert 'id="composer-input"' in response.text
    assert 'aria-live="polite"' in response.text
    assert "Dữ liệu tổng hợp" in response.text
    assert "Product" in response.text
    assert "Review" in response.text
    assert "Trust" in response.text
    assert "Market" in response.text
    assert "<style" not in response.text
    assert "onclick=" not in response.text


def test_frontend_assets_use_safe_same_origin_streaming_client() -> None:
    client = TestClient(create_app(frontend_settings()))

    script = client.get("/assets/app.js")
    styles = client.get("/assets/styles.css")
    favicon = client.get("/favicon.ico")

    assert script.status_code == 200
    assert styles.status_code == 200
    assert favicon.status_code == 204
    assert script.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert "/api/v1/chat/stream" in script.text
    assert "sessionStorage" in script.text
    assert "localStorage" not in script.text
    assert "thuong-tri.api-key" not in script.text
    assert "innerHTML" not in script.text
    assert "textContent" in script.text
    assert "AbortController" in script.text
    assert "await reader.cancel()" in script.text
    assert "requestGeneration" in script.text
    assert "response.body.getReader" in script.text
    assert "prefers-reduced-motion" in styles.text
    assert "@media (max-width: 780px)" in styles.text


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
        knowledge_backend="qdrant",
        qdrant_api_key="strong-qdrant-key",
        operations_api_key="strong-operations-key",
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
    project_root = Path(__file__).resolve().parents[1]
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert '"app.frontend" = ["index.html", "assets/*.css", "assets/*.js"]' in (
        pyproject
    )


def frontend_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        gateway_api_keys="frontend:frontend-secret-key",
        legacy_chat_enabled=True,
    )
