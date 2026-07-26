import pytest
from pydantic import ValidationError

from mutiai.config import Settings


@pytest.mark.parametrize(
    "override",
    [
        {"database_auto_migrate": True, "bootstrap_admin_enabled": False},
        {"database_auto_migrate": False, "bootstrap_admin_enabled": True},
    ],
)
def test_production_rejects_automatic_bootstrap(override) -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="production", **override)


def test_production_accepts_explicit_release_settings() -> None:
    settings = Settings(
        app_env="production",
        database_auto_migrate=False,
        bootstrap_admin_enabled=False,
        runtime_security_mode="workspace_restricted",
    )

    assert settings.app_env == "production"


def test_production_rejects_demo_full_access() -> None:
    with pytest.raises(ValidationError, match="cannot use"):
        Settings(
            app_env="production",
            database_auto_migrate=False,
            bootstrap_admin_enabled=False,
            runtime_security_mode="demo_full_access",
        )


def test_non_default_fake_runtime_scenario_is_development_only() -> None:
    with pytest.raises(ValidationError, match="production"):
        Settings(
            app_env="production",
            database_auto_migrate=False,
            bootstrap_admin_enabled=False,
            runtime_security_mode="workspace_restricted",
            fake_runtime_scenario="wait_first_specialist",
        )

    with pytest.raises(ValidationError, match="requires RUNTIME_PROVIDER=fake"):
        Settings(
            runtime_provider="codex",
            fake_runtime_scenario="needs_revision",
        )


def test_demo_full_access_requires_loopback_binding() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(
            app_env="development",
            app_host="0.0.0.0",
            runtime_security_mode="demo_full_access",
        )

    settings = Settings(
        app_env="development",
        app_host="0.0.0.0",
        runtime_security_mode="workspace_restricted",
    )
    assert settings.app_host == "0.0.0.0"


@pytest.mark.parametrize(
    "override",
    [
        {"runtime_token_budget_limit": 100},
        {"runtime_token_reservation_per_execution": 25},
    ],
)
def test_runtime_token_budget_settings_must_be_configured_together(override) -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        Settings(**override)


def test_runtime_token_reservation_cannot_exceed_budget() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        Settings(
            runtime_token_budget_limit=100,
            runtime_token_reservation_per_execution=101,
        )
