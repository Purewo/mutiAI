import json
import time

from fastapi.testclient import TestClient

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import FakeRuntimeAdapter, RuntimeResult


class RichContentFakeRuntime(FakeRuntimeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.assistant_summary: dict | None = None

    def execute(self, **kwargs) -> RuntimeResult:
        if kwargs["role_key"] == "platform-assistant" and self.assistant_summary:
            return RuntimeResult(
                status="completed",
                runtime_job_id=f"fake:{kwargs['execution_id']}",
                summary=json.dumps(self.assistant_summary),
            )
        return super().execute(**kwargs)


def rich_content_app(tmp_path, runtime: RichContentFakeRuntime):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'assistant-rich-content.db'}",
        runtime_workspace_root=tmp_path / "runtime-workspaces",
        assistant_attachment_root=tmp_path / "assistant-attachments",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
        assistant_runtime_provider="inherit",
    )
    return create_app(settings, runtime_adapter=runtime)


def organization_spec() -> dict:
    return {
        "schema_version": "1.0",
        "name": "Rich Content Team",
        "description": "Exercises product-backed assistant diagrams.",
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
                "responsibility": "Complete bounded work",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


def wait_for_turn(client: TestClient, turn_id: str) -> None:
    for _ in range(100):
        response = client.get(f"/api/v1/assistant/turns/{turn_id}")
        assert response.status_code == 200
        if response.json()["status"] == "completed":
            return
        time.sleep(0.01)
    raise AssertionError("assistant Turn did not complete")


def test_runtime_hints_become_owner_checked_content_blocks(tmp_path) -> None:
    runtime = RichContentFakeRuntime()
    app = rich_content_app(tmp_path, runtime)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "123456"},
        )
        assert login.status_code == 200
        proposal = client.post(
            "/api/v1/organizations/proposals",
            json={
                "organization_id": None,
                "source_request": "Create a small team.",
                "spec": organization_spec(),
            },
        )
        assert proposal.status_code == 201
        organization_id = proposal.json()["organization_id"]
        spec_version_id = proposal.json()["spec_version_id"]

        runtime.assistant_summary = {
            "reply": "<script>alert('x')</script>**Organization ready.**",
            "action": None,
            "presentation_requests": [
                {
                    "kind": "resource_ref",
                    "resource_type": "organization",
                    "resource_id": organization_id,
                },
                {
                    "kind": "diagram",
                    "template": "organization_chart",
                    "source": {
                        "kind": "organization_spec_version",
                        "organization_id": organization_id,
                        "spec_version_id": spec_version_id,
                    },
                    "text": "The organization lead manages one specialist.",
                },
                {
                    "kind": "resource_ref",
                    "resource_type": "task",
                    "resource_id": "00000000-0000-0000-0000-000000000000",
                },
            ],
        }
        conversation = client.post("/api/v1/assistant/conversations")
        assert conversation.status_code == 201
        conversation_id = conversation.json()["conversation_id"]
        submitted = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "rich-content-message"},
            json={"text": "Show this organization."},
        )
        assert submitted.status_code == 202
        wait_for_turn(client, submitted.json()["turn"]["turn_id"])

        messages = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/messages"
        )
        assert messages.status_code == 200
        assistant_message = messages.json()["items"][-1]
        assert assistant_message["content_schema_version"] == "1.0"
        blocks = assistant_message["content_blocks"]
        assert [block["type"] for block in blocks] == [
            "markdown",
            "resource_ref",
            "diagram",
        ]
        assert "<script>" not in blocks[0]["text"]
        assert blocks[1]["resource_id"] == organization_id
        assert blocks[1]["label"] == "Rich Content Team"
        assert blocks[2]["source"] == {
            "kind": "organization_spec_version",
            "organization_id": organization_id,
            "spec_version_id": spec_version_id,
        }
