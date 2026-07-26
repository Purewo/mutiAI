import json
from pathlib import Path

from mutiai.api.schemas.assistant import AssistantEventResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_committed_assistant_event_schema_matches_model() -> None:
    path = PROJECT_ROOT / "contracts" / "events" / "assistant-event.v1.json"
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert committed == AssistantEventResponse.model_json_schema()
