"""Export versioned product and HTTP contract snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from mutiai.api.schemas.tasks import TaskEventResponse
from mutiai.config import Settings
from mutiai.domain import OrganizationSpec
from mutiai.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORGANIZATION_SCHEMA_PATH = (
    PROJECT_ROOT / "contracts" / "schemas" / "organization-spec.v1.json"
)
OPENAPI_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "openapi" / "openapi.v1.json"
TASK_EVENT_SCHEMA_PATH = (
    PROJECT_ROOT / "contracts" / "events" / "task-event.v1.json"
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")


def main() -> None:
    write_json(ORGANIZATION_SCHEMA_PATH, OrganizationSpec.model_json_schema())
    app = create_app(
        Settings(
            app_env="test",
            database_url="sqlite+pysqlite:///:memory:",
            database_auto_migrate=False,
            bootstrap_admin_enabled=False,
        )
    )
    write_json(OPENAPI_SCHEMA_PATH, app.openapi())
    write_json(TASK_EVENT_SCHEMA_PATH, TaskEventResponse.model_json_schema())


if __name__ == "__main__":
    main()
