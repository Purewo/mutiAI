"""Application configuration for the local and production-compatible core."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings with safe development defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65_535)
    database_url: str = "sqlite+pysqlite:///./var/mutiai.db"
    runtime_workspace_root: Path = Field(
        default=Path(r"G:\AI\AI_private\mutiAI-runtime-workspaces"),
        validation_alias=AliasChoices(
            "MUTIAI_RUNTIME_WORKSPACE_ROOT",
            "RUNTIME_WORKSPACE_ROOT",
        ),
    )
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: SecretStr = SecretStr("change-me-before-network-access")


def get_settings() -> Settings:
    """Create settings for an application instance."""

    return Settings()
