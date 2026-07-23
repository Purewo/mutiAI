import json
from pathlib import Path

from mutiai.config import Settings
from mutiai.main import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_committed_openapi_schema_matches_application() -> None:
    path = PROJECT_ROOT / "contracts" / "openapi" / "openapi.v1.json"
    committed = json.loads(path.read_text(encoding="utf-8"))
    app = create_app(
        Settings(
            app_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            database_auto_migrate=False,
            bootstrap_admin_enabled=False,
        )
    )

    assert committed == app.openapi()
