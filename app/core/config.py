"""Environment-backed application settings."""

import os
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
    google_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    )
    google_genai_use_vertexai: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "GOOGLE_GENAI_USE_VERTEXAI",
        ),
    )
    google_cloud_project: str | None = Field(
        default=None,
        validation_alias="GOOGLE_CLOUD_PROJECT",
    )
    google_cloud_location: str | None = Field(
        default=None,
        validation_alias="GOOGLE_CLOUD_LOCATION",
    )
    adk_model: str = Field(default="gemini-2.5-flash", validation_alias="ADK_MODEL")
    adk_app_name: str = Field(
        default="vietnamese_ecommerce_phase1",
        validation_alias="ADK_APP_NAME",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


settings = Settings()


def configure_google_environment(config: Settings = settings) -> None:
    """Expose non-secret ADK settings through the SDK's expected environment."""

    if config.google_api_key:
        os.environ.setdefault("GOOGLE_API_KEY", config.google_api_key)
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = str(
        config.google_genai_use_vertexai
    ).lower()
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = str(
        config.google_genai_use_vertexai
    ).lower()
    if config.google_cloud_project:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.google_cloud_project)
    if config.google_cloud_location:
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", config.google_cloud_location)


def public_settings(config: Settings = settings) -> dict[str, Any]:
    """Return safe settings for diagnostics without exposing credentials."""

    return {
        "database_driver": config.database_url.split(":", 1)[0],
        "adk_model": config.adk_model,
        "adk_app_name": config.adk_app_name,
        "google_genai_use_vertexai": config.google_genai_use_vertexai,
    }


configure_google_environment()
