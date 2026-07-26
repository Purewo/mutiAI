import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mutiai.api.errors import ApiError
from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import Task, User
from mutiai.runtime import FakeRuntimeAdapter
from mutiai.security import hash_password


def attachment_app(tmp_path, *, max_bytes: int = 10 * 1024 * 1024):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'assistant-attachments.db'}",
        runtime_workspace_root=tmp_path / "runtime-workspaces",
        assistant_attachment_root=tmp_path / "assistant-attachments",
        assistant_attachment_max_bytes=max_bytes,
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


def create_conversation(client: TestClient) -> str:
    response = client.post("/api/v1/assistant/conversations")
    assert response.status_code == 201
    return response.json()["conversation_id"]


def upload(
    client: TestClient,
    conversation_id: str,
    content: bytes,
    *,
    name: str,
    media_type: str,
):
    return client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/attachments",
        files={"file": (name, content, media_type)},
    )


def test_attachment_upload_attach_read_and_revoke_flow(tmp_path) -> None:
    app = attachment_app(tmp_path)

    with TestClient(app) as client:
        login(client)
        conversation_id = create_conversation(client)
        content = b'{"species": "setosa", "count": 50}\n'
        digest = hashlib.sha256(content).hexdigest()

        uploaded = upload(
            client,
            conversation_id,
            content,
            name="iris.json",
            media_type="application/json",
        )
        assert uploaded.status_code == 201
        attachment = uploaded.json()
        attachment_id = attachment["attachment_id"]
        assert attachment["status"] == "uploaded"
        assert attachment["sha256"] == digest
        assert "storage_relative_path" not in attachment

        before_message = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/attachments/{attachment_id}/content"
        )
        assert before_message.status_code == 200
        assert before_message.content == content

        submitted = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "attachment-message-1"},
            json={
                "text": "Read this JSON file.",
                "attachment_refs": [{"attachment_id": attachment_id}],
            },
        )
        assert submitted.status_code == 202
        message = submitted.json()["message"]
        assert message["attachment_refs"][0]["attachment_id"] == attachment_id
        assert any(
            block["type"] == "attachment" and block["attachment_id"] == attachment_id
            for block in message["content_blocks"]
        )

        readable = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/attachments/{attachment_id}/content"
        )
        assert readable.status_code == 200
        assert readable.headers["content-type"].startswith("application/json")
        assert readable.headers["etag"] == f'"{digest}"'
        assert readable.headers["x-content-sha256"] == digest
        assert readable.headers["x-content-type-options"] == "nosniff"
        assert readable.content == content

        with app.state.database.session() as session:
            user_id = session.scalar(select(User.user_id).where(User.username == "admin"))
            assert user_id is not None
            tool_result = app.state.platform_assistant._call_product_tool(
                session,
                tool="mutiai_get_attachment_content",
                arguments={"attachment_id": attachment_id},
                conversation_id=conversation_id,
                owner_user_id=user_id,
                turn_id=submitted.json()["turn"]["turn_id"],
            )
            assert session.scalars(select(Task)).all() == []

        assert tool_result["content_format"] == "json"
        assert tool_result["content"] == {"species": "setosa", "count": 50}

        downloaded = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/attachments/{attachment_id}/content",
            params={"download": "true"},
        )
        assert downloaded.status_code == 200
        assert (
            'attachment; filename="iris.json"'
            in downloaded.headers["content-disposition"]
        )

        cannot_revoke = client.delete(
            f"/api/v1/assistant/conversations/{conversation_id}/attachments/{attachment_id}"
        )
        assert cannot_revoke.status_code == 409
        assert cannot_revoke.json()["code"] == "ASSISTANT_ATTACHMENT_NOT_REVOCABLE"

        uncommitted = upload(
            client,
            conversation_id,
            b"draft,not,sent\n",
            name="draft.csv",
            media_type="text/csv",
        )
        assert uncommitted.status_code == 201
        draft_id = uncommitted.json()["attachment_id"]
        revoked = client.delete(
            f"/api/v1/assistant/conversations/{conversation_id}/attachments/{draft_id}"
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"


def test_attachment_limits_and_binary_content_are_fail_closed(tmp_path) -> None:
    app = attachment_app(tmp_path, max_bytes=4)

    with TestClient(app) as client:
        login(client)
        conversation_id = create_conversation(client)

        too_large = upload(
            client,
            conversation_id,
            b"12345",
            name="large.txt",
            media_type="text/plain",
        )
        assert too_large.status_code == 413
        assert too_large.json()["code"] == "ASSISTANT_ATTACHMENT_TOO_LARGE"

        unsupported = upload(
            client,
            conversation_id,
            b"<html></html>",
            name="page.html",
            media_type="text/html",
        )
        assert unsupported.status_code == 415
        assert unsupported.json()["code"] == "ASSISTANT_ATTACHMENT_MEDIA_UNSUPPORTED"


def test_binary_attachment_cannot_be_read_by_assistant_content_tool(tmp_path) -> None:
    app = attachment_app(tmp_path)

    with TestClient(app) as client:
        login(client)
        conversation_id = create_conversation(client)
        uploaded = upload(
            client,
            conversation_id,
            b"\x89PNG\r\n\x1a\n",
            name="image.png",
            media_type="image/png",
        )
        assert uploaded.status_code == 201
        attachment_id = uploaded.json()["attachment_id"]
        submitted = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "attachment-image-message"},
            json={
                "text": "Inspect the image metadata.",
                "attachment_refs": [{"attachment_id": attachment_id}],
            },
        )
        assert submitted.status_code == 202

        binary_response = client.get(
            f"/api/v1/assistant/conversations/{conversation_id}/attachments/{attachment_id}/content"
        )
        assert binary_response.status_code == 200
        assert binary_response.content == b"\x89PNG\r\n\x1a\n"

        with app.state.database.session() as session:
            user_id = session.scalar(select(User.user_id).where(User.username == "admin"))
            assert user_id is not None
            with pytest.raises(ApiError) as error:
                app.state.platform_assistant._call_product_tool(
                    session,
                    tool="mutiai_get_attachment_content",
                    arguments={"attachment_id": attachment_id},
                    conversation_id=conversation_id,
                    owner_user_id=user_id,
                    turn_id=submitted.json()["turn"]["turn_id"],
                )

        assert error.value.code == "ASSISTANT_ATTACHMENT_CONTENT_UNSUPPORTED"


def test_attachment_is_scoped_to_the_conversation_owner(tmp_path) -> None:
    app = attachment_app(tmp_path)

    with TestClient(app) as owner_client:
        login(owner_client)
        conversation_id = create_conversation(owner_client)
        uploaded = upload(
            owner_client,
            conversation_id,
            b"owner-only",
            name="owner.txt",
            media_type="text/plain",
        )
        assert uploaded.status_code == 201
        attachment_id = uploaded.json()["attachment_id"]

        with app.state.database.session() as session:
            session.add(
                User(
                    username="other",
                    password_hash=hash_password("other-password"),
                    display_name="Other",
                    is_active=True,
                )
            )
            session.commit()

        with TestClient(app) as other_client:
            other_login = other_client.post(
                "/api/v1/auth/login",
                json={"username": "other", "password": "other-password"},
            )
            assert other_login.status_code == 200
            response = other_client.get(
                f"/api/v1/assistant/conversations/{conversation_id}/attachments/{attachment_id}/content"
            )
            assert response.status_code == 404
            assert response.json()["code"] == "ASSISTANT_CONVERSATION_NOT_FOUND"


def test_attachment_content_reader_rejects_text_over_64_kib(tmp_path) -> None:
    app = attachment_app(tmp_path, max_bytes=128 * 1024)

    with TestClient(app) as client:
        login(client)
        conversation_id = create_conversation(client)
        uploaded = upload(
            client,
            conversation_id,
            b"x" * (64 * 1024 + 1),
            name="large.txt",
            media_type="text/plain",
        )
        assert uploaded.status_code == 201
        attachment_id = uploaded.json()["attachment_id"]
        submitted = client.post(
            f"/api/v1/assistant/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "large-attachment-message"},
            json={
                "text": "Read the attachment.",
                "attachment_refs": [{"attachment_id": attachment_id}],
            },
        )
        assert submitted.status_code == 202

        with app.state.database.session() as session:
            user_id = session.scalar(select(User.user_id).where(User.username == "admin"))
            assert user_id is not None
            with pytest.raises(ApiError) as error:
                app.state.platform_assistant._call_product_tool(
                    session,
                    tool="mutiai_get_attachment_content",
                    arguments={"attachment_id": attachment_id},
                    conversation_id=conversation_id,
                    owner_user_id=user_id,
                    turn_id=submitted.json()["turn"]["turn_id"],
                )

        assert error.value.code == "ASSISTANT_ATTACHMENT_CONTENT_TOO_LARGE"
