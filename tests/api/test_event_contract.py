import json
from pathlib import Path

from mutiai.api.schemas.tasks import TaskEventResponse


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_committed_task_event_schema_matches_model() -> None:
    path = PROJECT_ROOT / "contracts" / "events" / "task-event.v1.json"
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert committed == TaskEventResponse.model_json_schema()
