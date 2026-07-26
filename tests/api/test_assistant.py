import time

from fastapi.testclient import TestClient

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import FakeRuntimeAdapter


def assistant_app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'assistant.db'}",
        runtime_workspace_root=tmp_path / "runtime-workspaces",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
        assistant_runtime_provider="inherit",
    )
    return create_app(settings, runtime_adapter=FakeRuntimeAdapter())


def login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 200


def wait_for_terminal_turn(client: TestClient, turn_id: str) -> dict:
    for _ in range(100):
        response = client.get(f"/api/v1/assistant/turns/{turn_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.01)
    raise AssertionError("assistant Turn did not finish")


def test_conversation_message_turn_and_idempotency_flow(tmp_path) -> None:
    app = assistant_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/v1/assistant/conversations").status_code == 401
        login(client)

        created = client.post("/api/v1/assistant/conversations")
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]

        first = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "assistant-message-1"},
            json={"text": "Design a small API organization."},
        )
        assert first.status_code == 202
        turn_id = first.json()["turn"]["turn_id"]
        assert wait_for_terminal_turn(client, turn_id)["status"] == "completed"

        replay = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "assistant-message-1"},
            json={"text": "This different body must not create a second Turn."},
        )
        assert replay.status_code == 200
        assert replay.json()["turn"]["turn_id"] == turn_id

        messages = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            params={"limit": 1},
        ).json()
        assert len(messages["items"]) == 1
        assert messages["next_cursor"] is not None
        second_page = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            params={"limit": 10, "cursor": messages["next_cursor"]},
        ).json()
        assert second_page["items"][0]["role"] == "assistant"


def test_assistant_sse_reconnect_uses_event_identity(tmp_path) -> None:
    app = assistant_app(tmp_path)

    with TestClient(app) as client:
        login(client)
        conversation_id = client.post("/api/v1/assistant/conversations").json()[
            "conversation_id"
        ]
        first_stream = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/events"
        )
        assert first_stream.status_code == 200
        first_event_id = next(
            line.removeprefix("id: ")
            for line in first_stream.text.splitlines()
            if line.startswith("id: ")
        )

        submission = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "assistant-message-sse"},
            json={"text": "Show current organizations."},
        )
        wait_for_terminal_turn(client, submission.json()["turn"]["turn_id"])

        resumed = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/events",
            headers={"Last-Event-ID": first_event_id},
        )
        assert resumed.status_code == 200
        assert "assistant.message.accepted" in resumed.text
        assert first_event_id not in [
            line.removeprefix("id: ")
            for line in resumed.text.splitlines()
            if line.startswith("id: ")
        ]


def test_archived_conversation_rejects_new_messages(tmp_path) -> None:
    app = assistant_app(tmp_path)

    with TestClient(app) as client:
        login(client)
        conversation_id = client.post("/api/v1/assistant/conversations").json()[
            "conversation_id"
        ]
        archived = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/archive"
        )
        assert archived.json()["status"] == "archived"
        response = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={
                "Idempotency-Key": "archived-message",
                "Accept-Language": "zh-CN",
            },
            json={"text": "This must be rejected."},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "ASSISTANT_CONVERSATION_ARCHIVED"
        assert response.json()["message"] == (
            "小助理会话已归档，不能继续发送消息。"
        )
