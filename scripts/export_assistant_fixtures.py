"""Capture platform-assistant API Fixtures through the real FastAPI routes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from mutiai.api.schemas.assistant import (
    AssistantActionResponse,
    AssistantConversationResponse,
    AssistantMessagePage,
    AssistantSubmissionResponse,
    AssistantTurnResponse,
)
from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import FakeRuntimeAdapter, RuntimeResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "contracts" / "fixtures" / "assistant"


class AssistantFixtureRuntime(FakeRuntimeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.action = {
            "action_type": "task.cancel",
            "target_type": "task",
            "target_id": "fixture-task-id",
            "payload": {"task_id": "fixture-task-id"},
        }

    def execute(self, **kwargs) -> RuntimeResult:
        if kwargs.get("role_key") == "platform-assistant":
            execution_id = kwargs["execution_id"]
            return RuntimeResult(
                status="completed",
                runtime_job_id=f"fake:{execution_id}",
                summary=json.dumps(
                    {
                        "reply": "I prepared a product action for confirmation.",
                        "action": {
                            "action_type": self.action["action_type"],
                            "target_type": self.action["target_type"],
                            "target_id": self.action["target_id"],
                            "payload_json": json.dumps(self.action["payload"]),
                        },
                    }
                ),
            )
        return super().execute(**kwargs)


def write_fixture(name: str, value: object) -> None:
    path = OUTPUT_ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")


def login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    response.raise_for_status()


def wait_for_turn(client: TestClient, turn_id: str) -> dict:
    for _ in range(200):
        response = client.get(f"/api/v1/assistant/turns/{turn_id}")
        response.raise_for_status()
        value = response.json()
        if value["status"] in {"completed", "failed", "cancelled"}:
            return value
        time.sleep(0.01)
    raise RuntimeError("assistant fixture Turn did not complete")


def wait_for_action(client: TestClient, action_id: str) -> dict:
    for _ in range(200):
        response = client.get(f"/api/v1/assistant/actions/{action_id}")
        response.raise_for_status()
        value = response.json()
        if value["status"] in {"completed", "failed", "declined"}:
            return value
        time.sleep(0.01)
    raise RuntimeError("assistant fixture Action did not complete")


def organization_spec() -> dict:
    return {
        "schema_version": "1.0",
        "name": "Assistant Fixture Team",
        "description": "Produces captured assistant Action states",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": "Plan, review, and summarize",
                "is_lead": True,
                "reports_to": None,
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "specialist",
                "name": "Specialist",
                "responsibility": "Write concise text",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


def parse_sse(body: str) -> list[dict]:
    values: list[dict] = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        data = next(
            (
                line.removeprefix("data: ")
                for line in lines
                if line.startswith("data: ")
            ),
            None,
        )
        if data:
            values.append(json.loads(data))
    return values


def main() -> None:
    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        settings = Settings(
            app_env="test",
            database_url=f"sqlite+pysqlite:///{root / 'assistant-fixtures.db'}",
            runtime_workspace_root=root / "runtime-workspaces",
            bootstrap_admin_enabled=True,
            bootstrap_admin_username="admin",
            bootstrap_admin_password="123456",
        )
        runtime = AssistantFixtureRuntime()
        app = create_app(settings, runtime_adapter=runtime)
        with TestClient(app) as client:
            login(client)
            conversation_response = client.post("/api/v1/assistant/conversations")
            conversation_response.raise_for_status()
            conversation = AssistantConversationResponse.model_validate(
                conversation_response.json()
            )
            write_fixture(
                "conversation-created.json",
                conversation.model_dump(mode="json"),
            )

            empty_actions_response = client.get(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/actions"
            )
            empty_actions_response.raise_for_status()
            write_fixture("actions-empty.json", empty_actions_response.json())

            submission_response = client.post(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/messages",
                headers={"Idempotency-Key": "fixture-assistant-message-1"},
                json={"text": "List my current organizations."},
            )
            submission_response.raise_for_status()
            submission = AssistantSubmissionResponse.model_validate(
                submission_response.json()
            )
            write_fixture(
                "message-submitted.json",
                submission.model_dump(mode="json"),
            )

            completed = AssistantTurnResponse.model_validate(
                wait_for_turn(client, submission.turn.turn_id)
            )
            write_fixture("turn-completed.json", completed.model_dump(mode="json"))

            page_response = client.get(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/messages"
            )
            page_response.raise_for_status()
            page = AssistantMessagePage.model_validate(page_response.json())
            write_fixture("messages-page.json", page.model_dump(mode="json"))

            action_response = client.get(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/actions"
            )
            action_response.raise_for_status()
            actions = [
                AssistantActionResponse.model_validate(item).model_dump(mode="json")
                for item in action_response.json()
            ]
            write_fixture("actions-proposed.json", actions)
            declined_response = client.post(
                f"/api/v1/assistant/actions/{actions[0]['action_id']}/decision",
                json={"decision": "decline"},
            )
            declined_response.raise_for_status()
            declined = AssistantActionResponse.model_validate(declined_response.json())
            write_fixture(
                "action-declined.json",
                declined.model_dump(mode="json"),
            )

            proposal_response = client.post(
                "/api/v1/organizations/proposals",
                json={
                    "source_request": "Create the fixture organization",
                    "spec": organization_spec(),
                },
            )
            proposal_response.raise_for_status()
            proposal = proposal_response.json()
            runtime.action = {
                "action_type": "organization.confirm",
                "target_type": "organization_version",
                "target_id": proposal["spec_version_id"],
                "payload": {
                    "organization_id": proposal["organization_id"],
                    "spec_version_id": proposal["spec_version_id"],
                },
            }
            completed_submission = client.post(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/messages",
                headers={"Idempotency-Key": "fixture-assistant-message-2"},
                json={"text": "Confirm the prepared organization proposal."},
            )
            completed_submission.raise_for_status()
            completed_turn = completed_submission.json()["turn"]
            wait_for_turn(client, completed_turn["turn_id"])
            action_list = client.get(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/actions"
            )
            action_list.raise_for_status()
            completed_action_id = action_list.json()[-1]["action_id"]
            confirm_response = client.post(
                f"/api/v1/assistant/actions/{completed_action_id}/decision",
                json={"decision": "confirm"},
            )
            confirm_response.raise_for_status()
            completed_action = AssistantActionResponse.model_validate(
                wait_for_action(client, completed_action_id)
            )
            write_fixture(
                "action-completed.json",
                completed_action.model_dump(mode="json"),
            )

            runtime.action = {
                "action_type": "task.cancel",
                "target_type": "task",
                "target_id": "missing-fixture-task",
                "payload": {"task_id": "missing-fixture-task"},
            }
            failed_submission = client.post(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/messages",
                headers={"Idempotency-Key": "fixture-assistant-message-3"},
                json={"text": "Cancel the selected Task."},
            )
            failed_submission.raise_for_status()
            failed_turn = failed_submission.json()["turn"]
            wait_for_turn(client, failed_turn["turn_id"])
            action_list = client.get(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/actions"
            )
            action_list.raise_for_status()
            failed_action_id = action_list.json()[-1]["action_id"]
            failed_decision = client.post(
                f"/api/v1/assistant/actions/{failed_action_id}/decision",
                json={"decision": "confirm"},
            )
            failed_decision.raise_for_status()
            failed_action = AssistantActionResponse.model_validate(
                wait_for_action(client, failed_action_id)
            )
            write_fixture(
                "action-failed.json",
                failed_action.model_dump(mode="json"),
            )

            events_response = client.get(
                f"/api/v1/assistant/conversations/{conversation.conversation_id}/events"
            )
            events_response.raise_for_status()
            write_fixture("events.json", parse_sse(events_response.text))


if __name__ == "__main__":
    main()
