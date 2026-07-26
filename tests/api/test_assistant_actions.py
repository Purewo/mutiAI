import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mutiai.api.errors import ApiError
from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import (
    Artifact,
    AssistantActionStatus,
    AssistantConversation,
    AssistantEvent,
    AssistantMessage,
    AssistantTurn,
    Task,
)
from mutiai.models.base import new_id
from mutiai.models.task_plan import ArtifactStatus
from mutiai.runtime import FakeRuntimeAdapter


def assistant_app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'assistant-actions.db'}",
        runtime_workspace_root=tmp_path / "runtime-workspaces",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
    )
    return create_app(settings, runtime_adapter=FakeRuntimeAdapter())


def login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 200
    return response.json()["user"]["user_id"]


def organization_spec(*, specialist_responsibility: str = "Write concise text") -> dict:
    return {
        "schema_version": "1.0",
        "name": "Assistant Action Team",
        "description": "Exercises confirmed assistant actions",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": "Plan, delegate, review, and summarize",
                "is_lead": True,
                "reports_to": None,
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "specialist",
                "name": "Specialist",
                "responsibility": specialist_responsibility,
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


def create_conversation(client: TestClient) -> str:
    response = client.post("/api/v1/assistant/conversations")
    assert response.status_code == 201
    return response.json()["conversation_id"]


def create_action(app, conversation_id: str, owner_user_id: str, data: dict) -> str:
    with app.state.database.session() as session:
        conversation = session.get(AssistantConversation, conversation_id)
        assert conversation is not None
        action = app.state.platform_assistant._create_action(
            session,
            conversation,
            source_turn_id=None,
            owner_user_id=owner_user_id,
            action_data=data,
        )
        session.commit()
        return action.action_id


def wait_for_terminal_action(client: TestClient, action_id: str) -> dict:
    for _ in range(200):
        response = client.get(f"/api/v1/assistant/actions/{action_id}")
        assert response.status_code == 200
        action = response.json()
        if action["status"] in {"completed", "failed", "declined"}:
            return action
        time.sleep(0.01)
    raise AssertionError("assistant action did not finish")


def propose(client: TestClient, spec: dict) -> dict:
    response = client.post(
        "/api/v1/organizations/proposals",
        json={"source_request": "Create this organization", "spec": spec},
    )
    assert response.status_code == 201
    return response.json()


def publish(client: TestClient, spec: dict) -> dict:
    proposal = propose(client, spec)
    base = (
        f"/api/v1/organizations/{proposal['organization_id']}/versions/"
        f"{proposal['spec_version_id']}"
    )
    assert client.post(base + "/confirm").status_code == 200
    assert client.post(base + "/publish").status_code == 200
    return proposal


def test_action_decline_is_terminal_and_idempotent(tmp_path) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)
        action_id = create_action(
            app,
            conversation_id,
            owner_user_id,
            {
                "action_type": "organization.confirm",
                "target_type": "organization_version",
                "target_id": "unused-version",
                "payload": {},
            },
        )

        declined = client.post(
            f"/api/v1/assistant/actions/{action_id}/decision",
            json={"decision": "decline"},
        )
        assert declined.status_code == 200
        assert declined.json()["status"] == "declined"

        replay = client.post(
            f"/api/v1/assistant/actions/{action_id}/decision",
            json={"decision": "confirm"},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "declined"


def test_confirmed_organization_action_completes_asynchronously(tmp_path) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)
        proposal = propose(client, organization_spec())
        action_id = create_action(
            app,
            conversation_id,
            owner_user_id,
            {
                "action_type": "organization.confirm",
                "target_type": "organization_version",
                "target_id": proposal["spec_version_id"],
                "payload": {
                    "organization_id": proposal["organization_id"],
                    "spec_version_id": proposal["spec_version_id"],
                },
            },
        )

        accepted = client.post(
            f"/api/v1/assistant/actions/{action_id}/decision",
            json={"decision": "confirm"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] in {
            "confirmed",
            "executing",
            "completed",
        }
        completed = wait_for_terminal_action(client, action_id)
        assert completed["status"] == "completed"
        assert completed["result"]["status"] == "confirmed"

        replay = client.post(
            f"/api/v1/assistant/actions/{action_id}/decision",
            json={"decision": "confirm"},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "completed"


def test_feasibility_blocked_action_persists_failure(tmp_path) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)
        proposal = propose(
            client,
            organization_spec(
                specialist_responsibility="Edit and render long videos locally"
            ),
        )
        action_id = create_action(
            app,
            conversation_id,
            owner_user_id,
            {
                "action_type": "organization.confirm",
                "target_type": "organization_version",
                "target_id": proposal["spec_version_id"],
                "payload": {"spec_version_id": proposal["spec_version_id"]},
            },
        )

        response = client.post(
            f"/api/v1/assistant/actions/{action_id}/decision",
            json={"decision": "confirm"},
        )
        assert response.status_code == 200
        failed = wait_for_terminal_action(client, action_id)
        assert failed["status"] == "failed"
        assert failed["error_code"] == "FEASIBILITY_BLOCKED"

        checks = client.get(
            "/api/v1/organizations/"
            f"{proposal['organization_id']}/versions/"
            f"{proposal['spec_version_id']}/feasibility-checks"
        )
        assert checks.status_code == 200
        assert any(check["phase"] == "confirmation" for check in checks.json())


def test_task_submit_action_and_product_query_tools_use_persisted_truth(
    tmp_path,
) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)
        proposal = publish(client, organization_spec())
        action_id = create_action(
            app,
            conversation_id,
            owner_user_id,
            {
                "action_type": "task.submit",
                "target_type": "organization",
                "target_id": proposal["organization_id"],
                "payload": {
                    "organization_id": proposal["organization_id"],
                    "request": "Write a concise text summary",
                    "orchestration_mode": "legacy",
                    "capability_requirements": {},
                },
            },
        )

        accepted = client.post(
            f"/api/v1/assistant/actions/{action_id}/decision",
            json={"decision": "confirm"},
        )
        assert accepted.status_code == 200
        completed = wait_for_terminal_action(client, action_id)
        assert completed["status"] == "completed"
        task_id = completed["result"]["task_id"]
        assert completed["result"]["status"] == "completed"

        with app.state.database.session() as session:
            organization = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_get_organization",
                arguments={"organization_id": proposal["organization_id"]},
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                turn_id="query-test",
            )
            task = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_get_task",
                arguments={"task_id": task_id},
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                turn_id="query-test",
            )
            usage = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_get_task_usage",
                arguments={"task_id": task_id},
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                turn_id="query-test",
            )

        assert organization["published_version"]["spec"]["name"] == (
            "Assistant Action Team"
        )
        assert task["task"]["task_id"] == task_id
        assert usage["usage"]["execution_count"] > 0


def test_assistant_task_feasibility_preview_and_action_reads_are_scoped(
    tmp_path,
) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)
        proposal = publish(client, organization_spec())

        with app.state.database.session() as session:
            preview = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_check_task_feasibility",
                arguments={
                    "organization_id": proposal["organization_id"],
                    "request": "Write a concise text summary",
                    "capability_requirements": {},
                },
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                turn_id="preview-test",
            )
            assert preview["feasibility_check"]["outcome"] == "feasible"
            assert preview["feasibility_check"]["phase"] == "assistant_task_preview"

            action = app.state.platform_assistant._create_action(
                session,
                session.get(AssistantConversation, conversation_id),
                source_turn_id=None,
                owner_user_id=owner_user_id,
                action_data={
                    "action_type": "task.submit",
                    "target_type": "organization",
                    "target_id": proposal["organization_id"],
                    "payload": {
                        "organization_id": proposal["organization_id"],
                        "request": "Write a concise text summary",
                        "capability_requirements": {},
                        "feasibility_check_id": preview["feasibility_check"][
                            "feasibility_check_id"
                        ],
                    },
                },
            )
            session.commit()
            listed = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_list_actions",
                arguments={"status": "proposed", "limit": 100},
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                turn_id="action-list-test",
            )
            fetched = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_get_action",
                arguments={"action_id": action.action_id},
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                turn_id="action-get-test",
            )

        assert any(item["action_id"] == action.action_id for item in listed["actions"])
        assert fetched["action"]["action_id"] == action.action_id
        assert fetched["action"]["status"] == "proposed"


def test_failed_assistant_action_can_be_proposed_again_with_new_identity(tmp_path) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)
        action_data = {
            "action_type": "task.submit",
            "target_type": "organization",
            "target_id": "organization-1",
            "payload": {"organization_id": "organization-1", "request": "same"},
        }
        with app.state.database.session() as session:
            conversation = session.get(AssistantConversation, conversation_id)
            first = app.state.platform_assistant._create_action(
                session,
                conversation,
                source_turn_id=None,
                owner_user_id=owner_user_id,
                action_data=action_data,
            )
            first.status = AssistantActionStatus.FAILED
            first.error_code = "TEST_FAILURE"
            session.commit()

            second = app.state.platform_assistant._create_action(
                session,
                conversation,
                source_turn_id=None,
                owner_user_id=owner_user_id,
                action_data=action_data,
            )
            session.commit()

        assert second.action_id != first.action_id
        assert second.idempotency_key != first.idempotency_key


def test_assistant_reads_verified_released_json_artifact_content(tmp_path) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)
        proposal = publish(client, organization_spec())
        submitted = client.post(
            f"/api/v1/organizations/{proposal['organization_id']}/tasks",
            headers={"Idempotency-Key": "assistant-artifact-content-task"},
            json={"request": "Create a result for assistant content reading."},
        )
        assert submitted.status_code == 201
        task_id = submitted.json()["task_id"]
        content_payload = {"duplicate_rows": 1, "sepal_length_mean": 5.8433}
        content = json.dumps(content_payload, separators=(",", ":")).encode()

        with app.state.database.session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            artifact_id = "assistant-artifact-content"
            relative_path = (
                Path("users")
                / task.owner_user_id
                / "organizations"
                / task.organization_id
                / "tasks"
                / task.task_id
                / "artifacts"
                / artifact_id
                / "quality.json"
            )
            parent = app.state.workspace_manager.provision(relative_path.parent)
            (parent / "quality.json").write_bytes(content)
            session.add(
                Artifact(
                    artifact_id=artifact_id,
                    task_id=task.task_id,
                    origin="task_input",
                    source_delivery_id="assistant-content-delivery",
                    producer_assignment_id=None,
                    producer_plan_step_id=None,
                    source_workspace_id=None,
                    contract_key="iris.quality.v1",
                    schema_version="1.0",
                    artifact_version=1,
                    media_type="application/json",
                    file_name="quality.json",
                    source_relative_path="quality.json",
                    storage_relative_path=relative_path.as_posix(),
                    sha256=hashlib.sha256(content).hexdigest(),
                    byte_size=len(content),
                    status=ArtifactStatus.RELEASED,
                    validation_summary="Validated test JSON.",
                )
            )
            session.commit()

            result = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_get_artifact_content",
                arguments={"task_id": task_id, "artifact_id": artifact_id},
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                turn_id="artifact-content-test",
            )

        assert result["content"] == content_payload
        assert result["content_format"] == "json"
        assert result["complete"] is True
        assert result["artifact"]["sha256"] == hashlib.sha256(content).hexdigest()
        assert "storage_relative_path" not in result["artifact"]
        assert "source_workspace_id" not in result["artifact"]


def test_action_type_whitelist_rejects_unknown_mutation(tmp_path) -> None:
    app = assistant_app(tmp_path)
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)

        with pytest.raises(ApiError) as exc_info:
            create_action(
                app,
                conversation_id,
                owner_user_id,
                {
                    "action_type": "database.execute_sql",
                    "target_type": "database",
                    "target_id": None,
                    "payload": {},
                },
            )
        assert exc_info.value.code == "ASSISTANT_ACTION_INVALID"


def test_compaction_limit_rotates_only_the_current_assistant_generation(
    tmp_path,
) -> None:
    app = assistant_app(tmp_path)
    app.state.settings.assistant_thread_max_compactions = 2
    with TestClient(app) as client:
        owner_user_id = login(client)
        conversation_id = create_conversation(client)

        with app.state.database.session() as session:
            conversation = session.get(AssistantConversation, conversation_id)
            assert conversation is not None
            conversation.runtime_thread_id = "thread-generation-1"
            conversation.runtime_thread_generation = 1
            conversation.last_message_sequence += 1
            message = AssistantMessage(
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                sequence=conversation.last_message_sequence,
                role="user",
                status="accepted",
                text_content="Trigger rotation",
                content_blocks=[{"type": "text", "text": "Trigger rotation"}],
                attachment_refs=[],
                idempotency_key="rotation-message",
            )
            session.add(message)
            session.flush()
            session.add(
                AssistantTurn(
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                    source_message_id=message.message_id,
                    execution_id=new_id(),
                    idempotency_key="rotation-turn",
                    runtime_provider="codex",
                    runtime_thread_generation=1,
                    runtime_thread_id="thread-generation-1",
                    status="completed",
                    context_compactions=2,
                )
            )
            session.commit()

            app.state.platform_assistant._rotate_thread_if_needed(session, conversation)
            session.commit()
            session.refresh(conversation)
            event = session.scalar(
                select(AssistantEvent)
                .where(
                    AssistantEvent.conversation_id == conversation_id,
                    AssistantEvent.event_type
                    == "assistant.runtime_thread.rotation_requested",
                )
                .order_by(AssistantEvent.sequence.desc())
            )

        assert conversation.runtime_thread_id is None
        assert conversation.runtime_thread_generation == 1
        assert event is not None
        assert event.payload["reason"] == "compaction_limit"
