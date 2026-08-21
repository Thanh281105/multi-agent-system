"""Environment-backed application settings."""

from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Small configuration surface for the Phase 1 proof of concept."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = (
        "postgresql+psycopg://ecommerce:ecommerce@localhost:5432/ecommerce"
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias="OPENAI_API_KEY",
    )
    openai_model: str = Field(
        default="gpt-5.4-mini",
        validation_alias=AliasChoices("OPENAI_MODEL", "MODEL"),
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


settings = Settings()


def public_settings(config: Settings = settings) -> dict[str, Any]:
    """Return safe settings for diagnostics without exposing credentials."""

    return {
        "database_driver": config.database_url.split(":", 1)[0],
        "llm_provider": "openai",
        "openai_model": config.openai_model,
        "openai_configured": bool(config.openai_api_key),
    }
