import json
from pathlib import Path

from mutiai.api.schemas.assistant import (
    AssistantActionResponse,
    AssistantConversationResponse,
    AssistantEventResponse,
    AssistantMessagePage,
    AssistantSubmissionResponse,
    AssistantTurnResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "contracts" / "fixtures" / "assistant"


def load(name: str):
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_committed_assistant_fixtures_match_public_models() -> None:
    AssistantConversationResponse.model_validate(load("conversation-created.json"))
    AssistantSubmissionResponse.model_validate(load("message-submitted.json"))
    AssistantTurnResponse.model_validate(load("turn-completed.json"))
    AssistantMessagePage.model_validate(load("messages-page.json"))
    AssistantActionResponse.model_validate(load("action-declined.json"))
    AssistantActionResponse.model_validate(load("action-completed.json"))
    AssistantActionResponse.model_validate(load("action-failed.json"))

    for item in load("actions-proposed.json"):
        AssistantActionResponse.model_validate(item)
    for item in load("events.json"):
        AssistantEventResponse.model_validate(item)

    assert load("actions-empty.json") == []
