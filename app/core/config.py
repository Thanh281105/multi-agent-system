"""Environment-backed application settings with production guardrails."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime configuration; secret values stay redacted in reprs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = (
        "postgresql+psycopg://ecommerce:ecommerce@localhost:5432/ecommerce"
    )
    public_snapshot_dir: Path = Path("data/snapshots/tiki-books-v4-eval")
    app_env: Literal["development", "test", "production"] = Field(
        default="development",
        validation_alias="APP_ENV",
    )
    gateway_api_keys: SecretStr = Field(
        default=SecretStr("demo:demo-local-key"),
        validation_alias="GATEWAY_API_KEYS",
    )
    gateway_principal_policies: str = Field(
        default="demo:default:ecommerce.read",
        validation_alias="GATEWAY_PRINCIPAL_POLICIES",
    )
    operations_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="OPERATIONS_API_KEY",
    )
    gateway_rate_limit_requests: int = Field(
        default=30,
        ge=1,
        le=100_000,
        validation_alias="GATEWAY_RATE_LIMIT_REQUESTS",
    )
    gateway_rate_limit_window_seconds: int = Field(
        default=60,
        ge=1,
        le=86_400,
        validation_alias="GATEWAY_RATE_LIMIT_WINDOW_SECONDS",
    )
    gateway_auth_attempt_requests: int = Field(
        default=120,
        ge=1,
        le=100_000,
        validation_alias="GATEWAY_AUTH_ATTEMPT_REQUESTS",
    )
    session_ttl_seconds: int = Field(
        default=3_600,
        ge=60,
        le=2_592_000,
        validation_alias="SESSION_TTL_SECONDS",
    )
    shared_state_backend: Literal["memory", "redis"] = Field(
        default="memory",
        validation_alias="SHARED_STATE_BACKEND",
    )
    redis_url: SecretStr = Field(
        default=SecretStr("redis://localhost:6379/0"),
        validation_alias="REDIS_URL",
    )
    redis_key_prefix: str = Field(
        default="ecommerce_agents",
        pattern=r"^[a-zA-Z0-9:_-]{1,64}$",
        validation_alias="REDIS_KEY_PREFIX",
    )
    redis_socket_timeout_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=30,
        validation_alias="REDIS_SOCKET_TIMEOUT_SECONDS",
    )
    knowledge_backend: Literal["disabled", "qdrant"] = Field(
        default="disabled",
        validation_alias="KNOWLEDGE_BACKEND",
    )
    qdrant_url: str = Field(
        default="http://localhost:6333",
        validation_alias="QDRANT_URL",
    )
    qdrant_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="QDRANT_API_KEY",
    )
    qdrant_collection: str = Field(
        default="knowledge",
        pattern=r"^[a-zA-Z0-9_-]{1,128}$",
        validation_alias="QDRANT_COLLECTION",
    )
    qdrant_timeout_seconds: float = Field(
        default=3.0,
        ge=0.1,
        le=30,
        validation_alias="QDRANT_TIMEOUT_SECONDS",
    )
    orchestration_timeout_seconds: float = Field(
        default=30.0,
        ge=1,
        le=300,
        validation_alias="ORCHESTRATION_TIMEOUT_SECONDS",
    )
    legacy_chat_enabled: bool = Field(
        default=False,
        validation_alias="LEGACY_CHAT_ENABLED",
    )
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
    )
    openai_model: str = Field(
        default="gpt-5.4-mini",
        validation_alias=AliasChoices("OPENAI_MODEL", "MODEL"),
    )
    model_runtime_mode: Literal["off", "shadow", "hybrid", "required"] = Field(
        default="hybrid",
        validation_alias="MODEL_RUNTIME_MODE",
    )
    openai_routing_model: str = Field(
        default="gpt-5.4-nano",
        validation_alias="OPENAI_ROUTING_MODEL",
    )
    openai_planning_model: str = Field(
        default="gpt-5.4-mini",
        validation_alias="OPENAI_PLANNING_MODEL",
    )
    openai_specialist_model: str = Field(
        default="gpt-5.4-nano",
        validation_alias="OPENAI_SPECIALIST_MODEL",
    )
    openai_synthesis_model: str = Field(
        default="gpt-5.4-mini",
        validation_alias="OPENAI_SYNTHESIS_MODEL",
    )
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = Field(
        default="low",
        validation_alias="OPENAI_REASONING_EFFORT",
    )
    openai_request_timeout_seconds: float = Field(
        default=18.0,
        ge=1,
        le=120,
        validation_alias="OPENAI_REQUEST_TIMEOUT_SECONDS",
    )
    openai_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias="OPENAI_MAX_RETRIES",
    )
    openai_max_output_tokens: int = Field(
        default=1_200,
        ge=64,
        le=16_384,
        validation_alias="OPENAI_MAX_OUTPUT_TOKENS",
    )
    openai_max_concurrency: int = Field(
        default=8,
        ge=1,
        le=64,
        validation_alias="OPENAI_MAX_CONCURRENCY",
    )
    openai_circuit_failure_threshold: int = Field(
        default=4,
        ge=1,
        le=20,
        validation_alias="OPENAI_CIRCUIT_FAILURE_THRESHOLD",
    )
    openai_circuit_recovery_seconds: float = Field(
        default=30.0,
        ge=1,
        le=600,
        validation_alias="OPENAI_CIRCUIT_RECOVERY_SECONDS",
    )
    embedding_backend: Literal["auto", "hashing", "openai"] = Field(
        default="auto",
        validation_alias="EMBEDDING_BACKEND",
    )
    openai_embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias="OPENAI_EMBEDDING_MODEL",
    )
    openai_embedding_dimensions: int = Field(
        default=1_536,
        ge=32,
        le=3_072,
        validation_alias="OPENAI_EMBEDDING_DIMENSIONS",
    )
    v2_corpus_version_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{2,127}$",
        validation_alias="V2_CORPUS_VERSION_ID",
    )
    v2_index_manifest_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{2,127}$",
        validation_alias="V2_INDEX_MANIFEST_ID",
    )
    v2_budget_account_id: str = Field(
        default="thanh-v2-provider-global",
        pattern=r"^[a-z][a-z0-9_-]{2,127}$",
        validation_alias="V2_BUDGET_ACCOUNT_ID",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("knowledge_backend", mode="before")
    @classmethod
    def normalize_legacy_static_knowledge_backend(cls, value: Any) -> Any:
        """Map the removed static backend to the safe disabled state."""

        if isinstance(value, str) and value.casefold() == "static":
            return "disabled"
        return value

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.v2_index_manifest_id is not None and self.v2_corpus_version_id is None:
            raise ValueError("V2_INDEX_MANIFEST_ID requires V2_CORPUS_VERSION_ID")
        configured_keys = self.gateway_api_keys.get_secret_value().strip()
        if self.app_env == "production" and (
            not configured_keys
            or "demo-local-key" in configured_keys
            or "replace-with-" in configured_keys.casefold()
        ):
            raise ValueError("production requires non-default GATEWAY_API_KEYS")
        if self.app_env == "production" and any(
            not separator or len(secret.strip()) < 16
            for entry in configured_keys.split(",")
            for _, separator, secret in (entry.partition(":"),)
        ):
            raise ValueError(
                "production requires GATEWAY_API_KEYS secrets of at least 16 characters"
            )
        if self.app_env == "production" and self.legacy_chat_enabled:
            raise ValueError("production requires LEGACY_CHAT_ENABLED=false")
        if (
            self.app_env == "production"
            and len(self.operations_api_key.get_secret_value()) < 16
        ):
            raise ValueError("production requires a strong OPERATIONS_API_KEY")
        if self.app_env == "production" and self.shared_state_backend != "redis":
            raise ValueError("production requires SHARED_STATE_BACKEND=redis")
        if self.app_env == "production":
            database = urlsplit(self.database_url)
            if (
                not database.scheme.startswith("postgresql+")
                or not database.hostname
                or not database.username
                or not database.password
                or database.password == "ecommerce"
                or "replace-with-" in database.password.casefold()
            ):
                raise ValueError(
                    "production requires an authenticated PostgreSQL DATABASE_URL "
                    "without default or placeholder credentials"
                )
            redis = urlsplit(self.redis_url.get_secret_value())
            if (
                redis.scheme not in {"redis", "rediss"}
                or not redis.hostname
                or not redis.password
                or "replace-with-" in redis.password.casefold()
            ):
                raise ValueError(
                    "production requires an authenticated REDIS_URL without "
                    "placeholder credentials"
                )
        qdrant_key = self.qdrant_api_key.get_secret_value().strip()
        if (
            self.app_env == "production"
            and self.knowledge_backend == "qdrant"
            and (len(qdrant_key) < 16 or "replace-with-" in qdrant_key.casefold())
        ):
            raise ValueError(
                "production requires a strong non-placeholder QDRANT_API_KEY"
            )
        openai_key = self.openai_api_key_value
        if (
            self.app_env == "production"
            and self.model_runtime_mode != "off"
            and not openai_key
        ):
            raise ValueError(
                "production requires OPENAI_API_KEY when model runtime is enabled"
            )
        if self.model_runtime_mode == "required" and not openai_key:
            raise ValueError("MODEL_RUNTIME_MODE=required requires OPENAI_API_KEY")
        if self.embedding_backend == "openai" and not openai_key:
            raise ValueError("EMBEDDING_BACKEND=openai requires OPENAI_API_KEY")
        if (
            self.app_env == "production"
            and self.knowledge_backend == "qdrant"
            and self.embedding_backend == "auto"
            and not openai_key
        ):
            raise ValueError("production Qdrant auto embedding requires OPENAI_API_KEY")
        return self

    @property
    def openai_api_key_value(self) -> str:
        """Reveal the provider key only at an outbound client boundary."""

        if self.openai_api_key is None:
            return ""
        return self.openai_api_key.get_secret_value().strip()


settings = Settings()


def public_settings(config: Settings = settings) -> dict[str, Any]:
    """Return safe settings for diagnostics without exposing credentials."""

    return {
        "database_driver": config.database_url.split(":", 1)[0],
        "llm_provider": "openai",
        "openai_model": config.openai_model,
        "openai_configured": bool(config.openai_api_key_value),
        "model_runtime_mode": config.model_runtime_mode,
        "routing_model": config.openai_routing_model,
        "planning_model": config.openai_planning_model,
        "specialist_model": config.openai_specialist_model,
        "synthesis_model": config.openai_synthesis_model,
        "embedding_backend": config.embedding_backend,
        "embedding_model": config.openai_embedding_model,
        "embedding_dimensions": config.openai_embedding_dimensions,
        "app_env": config.app_env,
        "gateway_api_key_count": len(
            [
                entry
                for entry in config.gateway_api_keys.get_secret_value().split(",")
                if entry.strip()
            ]
        ),
        "operations_auth_configured": bool(
            config.operations_api_key.get_secret_value()
        ),
        "gateway_rate_limit_requests": config.gateway_rate_limit_requests,
        "gateway_rate_limit_window_seconds": (config.gateway_rate_limit_window_seconds),
        "gateway_auth_attempt_requests": config.gateway_auth_attempt_requests,
        "shared_state_backend": config.shared_state_backend,
        "knowledge_backend": config.knowledge_backend,
        "legacy_chat_enabled": config.legacy_chat_enabled,
    }
