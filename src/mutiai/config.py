"""Application configuration for the local and production-compatible core."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings with safe development defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65_535)
    database_url: str = "sqlite+pysqlite:///./var/mutiai.db"
    database_auto_migrate: bool = True
    langgraph_checkpoint_path: Path = Path("./var/langgraph-checkpoints.db")
    runtime_provider: Literal["fake", "codex"] = "fake"
    runtime_max_concurrent_executions: int = Field(default=2, ge=1, le=64)
    runtime_token_budget_limit: int | None = Field(default=None, ge=1)
    runtime_token_reservation_per_execution: int | None = Field(
        default=None,
        ge=1,
    )
    runtime_provider_capacity_cache_seconds: float = Field(
        default=30.0,
        ge=0,
        le=300,
    )
    runtime_default_binding_key: str = Field(
        default="codex-local-default",
        min_length=1,
        max_length=64,
    )
    runtime_security_mode: Literal[
        "demo_full_access",
        "workspace_restricted",
    ] = "demo_full_access"
    runtime_thread_max_compactions: int | None = Field(default=None, ge=1)
    runtime_workspace_root: Path = Field(
        default=Path(r"G:\AI\AI_private\mutiAI-runtime-workspaces"),
        validation_alias=AliasChoices(
            "MUTIAI_RUNTIME_WORKSPACE_ROOT",
            "RUNTIME_WORKSPACE_ROOT",
        ),
    )
    codex_app_server_endpoint: str = "ws://127.0.0.1:4500"
    codex_app_server_ready_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
    )
    codex_model: str | None = None
    codex_reasoning_effort: str | None = Field(default=None, max_length=32)
    assistant_runtime_provider: Literal["inherit", "fake", "codex"] = "inherit"
    assistant_model: str | None = Field(default=None, max_length=100)
    assistant_reasoning_effort: str | None = Field(default=None, max_length=32)
    assistant_thread_max_compactions: int | None = Field(default=None, ge=1)
    assistant_tool_contract_version: str = Field(
        default="1.0", min_length=1, max_length=20
    )
    bootstrap_admin_enabled: bool = True
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: SecretStr = SecretStr("change-me-before-network-access")
    session_cookie_name: str = "mutiai_session"
    session_ttl_seconds: int = Field(default=604_800, ge=300, le=31_536_000)

    @model_validator(mode="after")
    def reject_unsafe_production_bootstrap(self) -> Self:
        if self.app_env == "production" and self.database_auto_migrate:
            raise ValueError("production requires DATABASE_AUTO_MIGRATE=false")
        if self.app_env == "production" and self.bootstrap_admin_enabled:
            raise ValueError("production requires BOOTSTRAP_ADMIN_ENABLED=false")
        if self.app_env == "production" and self.runtime_security_mode == (
            "demo_full_access"
        ):
            raise ValueError(
                "production cannot use RUNTIME_SECURITY_MODE=demo_full_access"
            )
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if (
            self.runtime_security_mode == "demo_full_access"
            and self.app_host not in loopback_hosts
        ):
            raise ValueError("demo Full Access requires APP_HOST to remain on loopback")
        budget_values = (
            self.runtime_token_budget_limit,
            self.runtime_token_reservation_per_execution,
        )
        if (budget_values[0] is None) != (budget_values[1] is None):
            raise ValueError(
                "RUNTIME_TOKEN_BUDGET_LIMIT and "
                "RUNTIME_TOKEN_RESERVATION_PER_EXECUTION must be set together"
            )
        if (
            budget_values[0] is not None
            and budget_values[1] is not None
            and budget_values[1] > budget_values[0]
        ):
            raise ValueError(
                "RUNTIME_TOKEN_RESERVATION_PER_EXECUTION cannot exceed "
                "RUNTIME_TOKEN_BUDGET_LIMIT"
            )
        return self


def get_settings() -> Settings:
    """Create settings for an application instance."""

    return Settings()
