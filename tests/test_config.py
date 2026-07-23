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
    )

    assert settings.app_env == "production"
