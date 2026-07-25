import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import func, select

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import (
    Artifact,
    Assignment,
    ProductEvent,
    RuntimeExecution,
    Task,
    User,
)
from mutiai.models.task import TaskStatus
from mutiai.models.task_plan import ArtifactStatus
from mutiai.orchestration.task_graph import build_task_graph
from mutiai.runtime import FakeRuntimeAdapter, RuntimeCapacity, RuntimeTokenUsage
from mutiai.security import hash_password


def task_spec() -> dict:
    return {
        "schema_version": "1.0",
        "name": "Delivery Team",
        "description": "Complete the M1 task flow",
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
                "role_key": "backend",
                "name": "Backend Developer",
                "responsibility": "Implement backend behavior",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "test",
                "name": "Test Engineer",
                "responsibility": "Verify backend behavior",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


def task_settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}",
        langgraph_checkpoint_path=tmp_path / "checkpoints.db",
        runtime_workspace_root=tmp_path / "runtime-workspaces",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
    )


def login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 200


def publish_organization(client: TestClient) -> str:
    proposal = client.post(
        "/api/v1/organizations/proposals",
        json={"spec": task_spec()},
    )
    assert proposal.status_code == 201
    version = proposal.json()
    base = (
        f"/api/v1/organizations/{version['organization_id']}/versions/"
        f"{version['spec_version_id']}"
    )
    assert client.post(base + "/confirm").status_code == 200
    assert client.post(base + "/publish").status_code == 200
    return version["organization_id"]


def sse_payloads(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def persist_test_artifact(app, task_id: str) -> tuple[Artifact, Path, bytes]:
    content = b'{"accepted":true,"amount":100}'
    with app.state.database.session() as session:
        task = session.get(Task, task_id)
        assert task is not None
        artifact_id = "artifact-content-test"
        relative_path = (
            Path("users")
            / task.owner_user_id
            / "organizations"
            / task.organization_id
            / "tasks"
            / task.task_id
            / "artifacts"
            / artifact_id
            / "result.json"
        )
        parent = app.state.workspace_manager.provision(relative_path.parent)
        stored_path = parent / "result.json"
        stored_path.write_bytes(content)
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task.task_id,
            origin="task_input",
            source_delivery_id="artifact-content-delivery",
            producer_assignment_id=None,
            producer_plan_step_id=None,
            source_workspace_id=None,
            contract_key="test.result.v1",
            schema_version="1.0",
            artifact_version=1,
            media_type="application/json",
            file_name="result.json",
            source_relative_path="result.json",
            storage_relative_path=relative_path.as_posix(),
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            status=ArtifactStatus.RELEASED,
            validation_summary="Validated test JSON.",
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        session.expunge(artifact)
    return artifact, stored_path, content


def test_artifact_content_is_owned_verified_and_downloadable(tmp_path) -> None:
    settings = task_settings(tmp_path)
    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "artifact-content-task"},
            json={"request": "Produce a downloadable test result."},
        )
        assert submitted.status_code == 201
        task_id = submitted.json()["task_id"]
        artifact, stored_path, content = persist_test_artifact(app, task_id)

        task_response = client.get(f"/api/v1/tasks/{task_id}")
        artifact_payload = task_response.json()["artifacts"][0]
        content_url = (
            f"/api/v1/tasks/{task_id}/artifacts/{artifact.artifact_id}/content"
        )
        assert artifact_payload["content_url"] == content_url
        assert artifact_payload["download_url"] == f"{content_url}?download=true"

        inline = client.get(content_url)
        assert inline.status_code == 200
        assert inline.content == content
        assert inline.headers["content-type"] == "application/json"
        assert inline.headers["content-disposition"] == 'inline; filename="result.json"'
        assert inline.headers["etag"] == f'"{artifact.sha256}"'
        assert inline.headers["x-content-sha256"] == artifact.sha256

        download = client.get(f"{content_url}?download=true")
        assert download.status_code == 200
        assert download.headers["content-disposition"] == (
            'attachment; filename="result.json"'
        )

        with app.state.database.session() as session:
            stored_artifact = session.get(Artifact, artifact.artifact_id)
            assert stored_artifact is not None
            stored_artifact.status = ArtifactStatus.DRAFT
            session.commit()
        unreleased = client.get(content_url)
        assert unreleased.status_code == 409
        assert unreleased.json()["code"] == "ARTIFACT_NOT_RELEASED"
        with app.state.database.session() as session:
            stored_artifact = session.get(Artifact, artifact.artifact_id)
            assert stored_artifact is not None
            stored_artifact.status = ArtifactStatus.RELEASED
            session.commit()

        other_task = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "artifact-other-task"},
            json={"request": "Create another task boundary."},
        ).json()
        cross_task = client.get(
            f"/api/v1/tasks/{other_task['task_id']}/artifacts/"
            f"{artifact.artifact_id}/content"
        )
        assert cross_task.status_code == 404
        assert cross_task.json()["code"] == "ARTIFACT_NOT_FOUND"

        stored_path.write_bytes(b"tampered")
        corrupt = client.get(content_url)
        assert corrupt.status_code == 409
        assert corrupt.json()["code"] == "ARTIFACT_STORED_FILE_CORRUPT"

        with app.state.database.session() as session:
            intruder = User(
                username="artifact-intruder",
                password_hash=hash_password("123456"),
                display_name="Artifact Intruder",
            )
            session.add(intruder)
            session.commit()
        assert client.post("/api/v1/auth/logout").status_code == 204
        intruder_login = client.post(
            "/api/v1/auth/login",
            json={"username": "artifact-intruder", "password": "123456"},
        )
        assert intruder_login.status_code == 200
        hidden_artifact = client.get(content_url)
        assert hidden_artifact.status_code == 404
        assert hidden_artifact.json()["code"] == "TASK_NOT_FOUND"


def test_task_usage_aggregates_reported_and_budget_token_semantics(tmp_path) -> None:
    settings = task_settings(tmp_path)
    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "task-token-usage"},
            json={"request": "Measure token usage by assignment."},
        )
        assert submitted.status_code == 201
        task_id = submitted.json()["task_id"]

        with app.state.database.session() as session:
            records = session.scalars(
                select(RuntimeExecution)
                .join(Assignment)
                .where(Assignment.task_id == task_id)
                .order_by(Assignment.created_at, Assignment.assignment_id)
            ).all()
            assert len(records) == 3
            reported, unavailable, pending = records
            reported.usage_status = "reported"
            reported.reserved_tokens = 0
            reported.charged_tokens = 42
            reported.input_tokens = 30
            reported.cached_input_tokens = 10
            reported.output_tokens = 12
            reported.reasoning_output_tokens = 2
            reported.total_tokens = 42
            reported.requested_model = "requested-model"
            reported.actual_model = "actual-model"
            unavailable.usage_status = "unavailable"
            unavailable.reserved_tokens = 0
            unavailable.charged_tokens = 100
            unavailable.input_tokens = None
            unavailable.cached_input_tokens = None
            unavailable.output_tokens = None
            unavailable.reasoning_output_tokens = None
            unavailable.total_tokens = None
            pending.usage_status = "pending"
            pending.reserved_tokens = 7
            pending.charged_tokens = None
            session.commit()

        response = client.get(f"/api/v1/tasks/{task_id}/usage")
        assert response.status_code == 200
        payload = response.json()
        assert payload["task_id"] == task_id
        assert payload["execution_count"] == 3
        assert payload["reported_execution_count"] == 1
        assert payload["unavailable_execution_count"] == 1
        assert payload["pending_execution_count"] == 1
        assert payload["reserved_tokens"] == 7
        assert payload["charged_tokens"] == 142
        assert payload["input_tokens"] == 30
        assert payload["cached_input_tokens"] == 10
        assert payload["output_tokens"] == 12
        assert payload["reasoning_output_tokens"] == 2
        assert payload["observed_total_tokens"] == 42
        assert len(payload["assignments"]) == 3
        reported_payload = next(
            item
            for item in payload["assignments"]
            if item["usage_status"] == "reported"
        )
        assert reported_payload["requested_model"] == "requested-model"
        assert reported_payload["actual_model"] == "actual-model"
        assert reported_payload["total_tokens"] == 42

        with app.state.database.session() as session:
            intruder = User(
                username="intruder",
                password_hash=hash_password("123456"),
                display_name="Intruder",
            )
            session.add(intruder)
            session.commit()
        assert client.post("/api/v1/auth/logout").status_code == 204
        intruder_login = client.post(
            "/api/v1/auth/login",
            json={"username": "intruder", "password": "123456"},
        )
        assert intruder_login.status_code == 200
        hidden_usage = client.get(f"/api/v1/tasks/{task_id}/usage")
        assert hidden_usage.status_code == 404
        assert hidden_usage.json()["code"] == "TASK_NOT_FOUND"


def test_task_idempotency_graph_fanout_events_and_restart(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter()
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        task_url = f"/api/v1/organizations/{organization_id}/tasks"
        first = client.post(
            task_url,
            headers={"Idempotency-Key": "m1-task-1"},
            json={"request": "Implement and verify the smallest task flow."},
        )

        assert first.status_code == 201
        payload = first.json()
        task_id = payload["task_id"]
        assert payload["status"] == "completed"
        assert len(payload["assignments"]) == 3
        assert {item["agent_role_key"] for item in payload["assignments"]} == {
            "backend",
            "lead",
            "test",
        }
        assert all(
            item["runtime_execution"]["provider"] == "fake"
            for item in payload["assignments"]
        )
        assert all(
            item["runtime_execution"]["thread_id"] is None
            and item["runtime_execution"]["workspace_id"] is None
            for item in payload["assignments"]
        )
        execution_ids = [item["execution_id"] for item in payload["assignments"]]
        assert all(
            adapter.call_count(execution_id) == 1 for execution_id in execution_ids
        )
        assert settings.langgraph_checkpoint_path.exists()
        with sqlite3.connect(settings.langgraph_checkpoint_path) as connection:
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
                (task_id,),
            ).fetchone()[0]
        assert checkpoint_count > 0
        assert not settings.runtime_workspace_root.exists()
        replay = client.post(
            task_url,
            headers={"Idempotency-Key": "m1-task-1"},
            json={"request": "Implement and verify the smallest task flow."},
        )
        assert replay.status_code == 200
        assert replay.json()["task_id"] == task_id
        assert all(
            adapter.call_count(execution_id) == 1 for execution_id in execution_ids
        )

        conflict = client.post(
            task_url,
            headers={"Idempotency-Key": "m1-task-1"},
            json={"request": "A different request must not reuse the key."},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "TASK_IDEMPOTENCY_CONFLICT"

        events_response = client.get(f"/api/v1/tasks/{task_id}/events")
        assert events_response.status_code == 200
        assert events_response.headers["content-type"].startswith("text/event-stream")
        events = sse_payloads(events_response)
        assert [event["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )
        assert events[0]["event_type"] == "task.created"
        assert events[-1]["event_type"] == "task.completed"
        assert "lead.review_requested" in {
            event["event_type"] for event in events
        }
        assert "lead.review_completed" in {
            event["event_type"] for event in events
        }

        resumed_events = client.get(
            f"/api/v1/tasks/{task_id}/events",
            headers={"Last-Event-ID": events[0]["event_id"]},
        )
        assert len(sse_payloads(resumed_events)) == len(events) - 1
        invalid_cursor = client.get(
            f"/api/v1/tasks/{task_id}/events",
            headers={"Last-Event-ID": "missing-event"},
        )
        assert invalid_cursor.status_code == 409
        assert invalid_cursor.json()["code"] == "TASK_EVENT_CURSOR_INVALID"

    with app.state.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(func.count()).select_from(Assignment)) == 3
        assert session.scalar(select(func.count()).select_from(RuntimeExecution)) == 3
        event_count = session.scalar(select(func.count()).select_from(ProductEvent))
        assert event_count is not None
        assert event_count > 16

        task = session.get(Task, task_id)
        completion_event = session.scalar(
            select(ProductEvent).where(
                ProductEvent.task_id == task_id,
                ProductEvent.event_type == "task.completed",
            )
        )
        assert task is not None
        assert completion_event is not None
        task.status = TaskStatus.RUNNING
        task.result_summary = None
        task.completed_at = None
        session.delete(completion_event)
        session.commit()

    restarted_adapter = FakeRuntimeAdapter()
    restarted_app = create_app(settings, runtime_adapter=restarted_adapter)
    with TestClient(restarted_app) as client:
        login(client)
        replay_after_restart = client.post(
            task_url,
            headers={"Idempotency-Key": "m1-task-1"},
            json={"request": "Implement and verify the smallest task flow."},
        )

    assert replay_after_restart.status_code == 200
    assert replay_after_restart.json()["task_id"] == task_id
    assert replay_after_restart.json()["status"] == "completed"
    assert all(
        restarted_adapter.call_count(execution_id) == 0
        for execution_id in execution_ids
    )


def test_role_runtime_bindings_are_frozen_on_each_execution(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter()
    app = create_app(settings, runtime_adapter=adapter)
    with TestClient(app) as client:
        login(client)
        bindings = {
            "lead-binding": ("lead-model-test", "high"),
            "backend-binding": ("backend-model-test", "medium"),
            "test-binding": ("test-model-test", "low"),
        }
        for key, (model, effort) in bindings.items():
            response = client.put(
                f"/api/v1/runtime/bindings/{key}",
                json={
                    "provider": "fake",
                    "model": model,
                    "reasoning_effort": effort,
                    "security_mode": "workspace_restricted",
                },
            )
            assert response.status_code == 200

        spec = task_spec()
        binding_by_role = {
            "lead": "lead-binding",
            "backend": "backend-binding",
            "test": "test-binding",
        }
        for role in spec["roles"]:
            role["runtime_binding_key"] = binding_by_role[role["role_key"]]
        proposal = client.post(
            "/api/v1/organizations/proposals",
            json={"spec": spec},
        )
        assert proposal.status_code == 201
        version = proposal.json()
        version_url = (
            f"/api/v1/organizations/{version['organization_id']}/versions/"
            f"{version['spec_version_id']}"
        )
        assert client.post(version_url + "/confirm").status_code == 200
        assert client.post(version_url + "/publish").status_code == 200

        submitted = client.post(
            f"/api/v1/organizations/{version['organization_id']}/tasks",
            headers={"Idempotency-Key": "role-binding-snapshot"},
            json={"request": "Run role-specific bounded work."},
        )
        assert submitted.status_code == 201
        executions = {
            item["agent_role_key"]: item["runtime_execution"]
            for item in submitted.json()["assignments"]
        }
        for binding_key, (model, effort) in bindings.items():
            role = next(
                role_name
                for role_name, configured_key in binding_by_role.items()
                if configured_key == binding_key
            )
            execution = executions[role]
            assert execution["runtime_binding_key"] == binding_key
            assert execution["requested_model"] == model
            assert execution["actual_model"] is None
            assert execution["reasoning_effort"] == effort
            assert execution["security_mode"] == "workspace_restricted"
            assert execution["approval_policy"] == "on-request"
            assert execution["sandbox_mode"] == "workspace-write"
            config = adapter.runtime_config_for(execution["execution_id"])
            assert config is not None
            assert config.binding_key == binding_key
            assert config.model == model
            assert config.reasoning_effort == effort


def test_task_requires_idempotency_key_and_published_organization(tmp_path) -> None:
    settings = task_settings(tmp_path)
    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())

    with TestClient(app) as client:
        login(client)
        proposal = client.post(
            "/api/v1/organizations/proposals",
            json={"spec": task_spec()},
        )
        assert proposal.status_code == 201
        organization_id = proposal.json()["organization_id"]
        task_url = f"/api/v1/organizations/{organization_id}/tasks"

        missing_key = client.post(
            task_url,
            json={"request": "This request has no idempotency key."},
        )
        assert missing_key.status_code == 422
        assert missing_key.json()["code"] == "INVALID_REQUEST"

        unpublished = client.post(
            task_url,
            headers={"Idempotency-Key": "unpublished-task"},
            json={"request": "This organization is not published."},
        )
        assert unpublished.status_code == 409
        assert unpublished.json()["code"] == "ORGANIZATION_NOT_PUBLISHED"
        assert not settings.langgraph_checkpoint_path.exists()
        assert not settings.runtime_workspace_root.exists()


def test_task_rejects_unconfigured_role_runtime_binding(tmp_path) -> None:
    settings = task_settings(tmp_path)
    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client)
        spec = task_spec()
        backend = next(
            role for role in spec["roles"] if role["role_key"] == "backend"
        )
        backend["runtime_binding_key"] = "missing-binding"
        proposal = client.post(
            "/api/v1/organizations/proposals",
            json={"spec": spec},
        )
        version = proposal.json()
        version_url = (
            f"/api/v1/organizations/{version['organization_id']}/versions/"
            f"{version['spec_version_id']}"
        )
        assert client.post(version_url + "/confirm").status_code == 200
        assert client.post(version_url + "/publish").status_code == 200
        response = client.post(
            f"/api/v1/organizations/{version['organization_id']}/tasks",
            headers={"Idempotency-Key": "missing-runtime-binding"},
            json={"request": "Reject before Runtime submission."},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "RUNTIME_BINDING_INVALID"


def test_failed_parallel_branch_resumes_without_replaying_success(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(fail_once_role_keys={"backend"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app, raise_server_exceptions=False) as client:
        login(client)
        initial_binding = client.put(
            "/api/v1/runtime/bindings/codex-local-default",
            json={
                "provider": "fake",
                "model": "initial-model-test",
                "reasoning_effort": "medium",
                "security_mode": "workspace_restricted",
            },
        )
        assert initial_binding.status_code == 200
        organization_id = publish_organization(client)
        task_url = f"/api/v1/organizations/{organization_id}/tasks"
        first = client.post(
            task_url,
            headers={"Idempotency-Key": "retry-task"},
            json={"request": "Verify pending writes recovery."},
        )

        assert first.status_code == 500
        assert first.json()["code"] == "INTERNAL_ERROR"

        with app.state.database.session() as session:
            task = session.scalar(select(Task))
            assignments = session.scalars(
                select(Assignment).order_by(Assignment.agent_role_key)
            ).all()
            assert task is not None
            assert task.status == "failed"
            execution_by_role = {
                assignment.agent_role_key: assignment.execution_id
                for assignment in assignments
            }

        assert adapter.call_count(execution_by_role["backend"]) == 1
        assert adapter.call_count(execution_by_role["test"]) == 1
        first_config = adapter.runtime_config_for(execution_by_role["backend"])
        assert first_config is not None
        assert first_config.model == "initial-model-test"

        replay = client.post(
            task_url,
            headers={"Idempotency-Key": "retry-task"},
            json={"request": "Verify pending writes recovery."},
        )

        assert replay.status_code == 200
        assert replay.json()["status"] == "failed"
        assert adapter.call_count(execution_by_role["backend"]) == 1
        assert adapter.call_count(execution_by_role["test"]) == 1

        changed_binding = client.put(
            "/api/v1/runtime/bindings/codex-local-default",
            json={
                "provider": "fake",
                "model": "changed-model-test",
                "reasoning_effort": "high",
                "security_mode": "demo_full_access",
            },
        )
        assert changed_binding.status_code == 200

        resumed = client.post(f"/api/v1/tasks/{task.task_id}/retry")

        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"
        assert len(resumed.json()["assignments"]) == 3
        assert adapter.call_count(execution_by_role["backend"]) == 2
        assert adapter.call_count(execution_by_role["test"]) == 1
        retry_config = adapter.runtime_config_for(execution_by_role["backend"])
        assert retry_config is not None
        assert retry_config.model == "initial-model-test"
        assert retry_config.reasoning_effort == "medium"
        assert retry_config.security_mode == "workspace_restricted"

        events = sse_payloads(
            client.get(f"/api/v1/tasks/{resumed.json()['task_id']}/events")
        )
        assert "runtime.execution_failed" in {event["event_type"] for event in events}
        assert "runtime.execution_retry_requested" in {
            event["event_type"] for event in events
        }
        assert "task.retry_requested" in {event["event_type"] for event in events}
        assert events[-1]["event_type"] == "task.completed"
        assert [event["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )


def test_retry_rejects_non_failed_task(tmp_path) -> None:
    settings = task_settings(tmp_path)
    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "completed-task-retry"},
            json={"request": "Complete before retry is requested."},
        )

        assert submitted.status_code == 201
        assert submitted.json()["status"] == "completed"
        rejected = client.post(
            f"/api/v1/tasks/{submitted.json()['task_id']}/retry"
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "TASK_NOT_RETRYABLE"


def test_cancel_waiting_task_preserves_completed_parallel_branch(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(wait_once_role_keys={"backend"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "cancel-waiting-task"},
            json={"request": "Cancel one waiting Runtime branch."},
        )

        assert submitted.status_code == 201
        task_id = submitted.json()["task_id"]
        before_by_role = {
            item["agent_role_key"]: item for item in submitted.json()["assignments"]
        }
        assert before_by_role["backend"]["status"] == "waiting"
        assert before_by_role["test"]["status"] == "completed"

        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")

        assert cancelled.status_code == 200
        payload = cancelled.json()
        assert payload["status"] == "cancelled"
        assert payload["completed_at"] is not None
        after_by_role = {
            item["agent_role_key"]: item for item in payload["assignments"]
        }
        assert after_by_role["backend"]["status"] == "cancelled"
        assert after_by_role["backend"]["runtime_execution"]["status"] == (
            "cancelled"
        )
        assert after_by_role["test"]["status"] == "completed"
        assert after_by_role["test"]["runtime_execution"]["status"] == "completed"
        assert adapter.was_cancelled(before_by_role["backend"]["execution_id"])

        events = sse_payloads(client.get(f"/api/v1/tasks/{task_id}/events"))
        event_types = [event["event_type"] for event in events]
        assert event_types.count("task.cancellation_requested") == 1
        assert event_types.count("runtime.execution_cancel_requested") == 1
        assert event_types.count("runtime.execution_interrupt_requested") == 1
        assert event_types.count("task.cancelled") == 1
        assert "runtime.execution_failed" not in event_types

        late_completion = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=before_by_role["backend"]["execution_id"],
            runtime_event_id="fake:late-completion",
            summary="This late result must not resume the cancelled graph.",
        )
        late_failure = app.state.task_orchestrator.fail_runtime_execution(
            execution_id=before_by_role["backend"]["execution_id"],
            runtime_event_id="fake:late-failure",
            terminal_status="failed",
            error="This late failure must not replace cancellation.",
        )
        assert late_completion.status == "cancelled"
        assert late_failure.status == "cancelled"
        after_late_events = sse_payloads(
            client.get(f"/api/v1/tasks/{task_id}/events")
        )
        assert not any(
            event["runtime_execution_id"]
            == before_by_role["backend"]["runtime_execution"][
                "runtime_execution_id"
            ]
            and event["event_type"]
            in {"runtime.execution_completed", "runtime.execution_failed"}
            for event in after_late_events
        )

        duplicate = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert duplicate.status_code == 200
        duplicate_events = sse_payloads(
            client.get(f"/api/v1/tasks/{task_id}/events")
        )
        assert len(duplicate_events) == len(after_late_events)


def test_cancel_rejects_completed_task(tmp_path) -> None:
    settings = task_settings(tmp_path)
    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "completed-task-cancel"},
            json={"request": "Complete before cancellation is requested."},
        )

        assert submitted.status_code == 201
        rejected = client.post(
            f"/api/v1/tasks/{submitted.json()['task_id']}/cancel"
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "TASK_NOT_CANCELLABLE"


def test_cancel_reports_unconfirmed_runtime_interrupt(tmp_path) -> None:
    class UnownedRuntimeAdapter(FakeRuntimeAdapter):
        def cancel(self, execution_id: str) -> bool:
            del execution_id
            return False

    settings = task_settings(tmp_path)
    adapter = UnownedRuntimeAdapter(wait_once_role_keys={"backend"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "unconfirmed-cancel"},
            json={"request": "Expose an unconfirmed Runtime interruption."},
        )
        task_id = submitted.json()["task_id"]

        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")

        assert cancelled.status_code == 409
        assert cancelled.json()["code"] == "TASK_CANCELLATION_INCOMPLETE"
        assert cancelled.json()["details"]["failures"]
        persisted = client.get(f"/api/v1/tasks/{task_id}")
        assert persisted.status_code == 200
        assert persisted.json()["status"] == "cancelled"
        event_types = {
            event["event_type"]
            for event in sse_payloads(
                client.get(f"/api/v1/tasks/{task_id}/events")
            )
        }
        assert "runtime.execution_cancel_failed" in event_types
        assert "runtime.execution_interrupt_requested" not in event_types

        duplicate = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "TASK_CANCELLATION_INCOMPLETE"


def test_waiting_runtime_completion_resumes_graph_idempotently(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(wait_once_role_keys={"backend"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        task_url = f"/api/v1/organizations/{organization_id}/tasks"
        submitted = client.post(
            task_url,
            headers={"Idempotency-Key": "waiting-task"},
            json={"request": "Wait for one external Runtime completion event."},
        )

        assert submitted.status_code == 201
        payload = submitted.json()
        task_id = payload["task_id"]
        assert payload["status"] == "waiting"
        assignment_by_role = {
            item["agent_role_key"]: item for item in payload["assignments"]
        }
        backend = assignment_by_role["backend"]
        test_assignment = assignment_by_role["test"]
        assert backend["status"] == "waiting"
        assert backend["runtime_execution"]["status"] == "waiting"
        assert test_assignment["status"] == "completed"
        assert test_assignment["runtime_execution"]["status"] == "completed"
        assert adapter.call_count(backend["execution_id"]) == 1
        assert adapter.call_count(test_assignment["execution_id"]) == 1

        config = {"configurable": {"thread_id": task_id}}
        with SqliteSaver.from_conn_string(
            str(settings.langgraph_checkpoint_path)
        ) as saver:
            graph = build_task_graph(
                lambda work: None,
                lambda task_id, results: None,
            ).compile(checkpointer=saver)
            snapshot = graph.get_state(config)
            assert any(
                interrupt.value.get("execution_id") == backend["execution_id"]
                for interrupt in snapshot.interrupts
            )

        completed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=backend["execution_id"],
            runtime_event_id="fake-runtime-event-1",
            runtime_job_id=backend["runtime_execution"]["runtime_job_id"],
            last_event_position="1",
            summary="backend completed after external event",
            usage=RuntimeTokenUsage(
                input_tokens=30,
                cached_input_tokens=10,
                output_tokens=12,
                reasoning_output_tokens=2,
                total_tokens=42,
            ),
        )

        assert completed.status == "completed"
        final = client.get(f"/api/v1/tasks/{task_id}")
        assert final.status_code == 200
        final_payload = final.json()
        assert final_payload["status"] == "completed"
        assert final_payload["result_summary"] == (
            "The organization lead accepted the specialist deliveries."
        )
        lead = next(
            assignment
            for assignment in final_payload["assignments"]
            if assignment["agent_role_key"] == "lead"
        )
        assert "backend completed after external event" in lead["instructions"]
        assert adapter.call_count(backend["execution_id"]) == 1
        assert adapter.call_count(test_assignment["execution_id"]) == 1
        final_backend = next(
            item
            for item in final_payload["assignments"]
            if item["agent_role_key"] == "backend"
        )
        assert final_backend["runtime_execution"]["usage_status"] == "reported"
        assert final_backend["runtime_execution"]["total_tokens"] == 42
        assert final_backend["runtime_execution"]["charged_tokens"] == 42

        controls = client.get("/api/v1/runtime/controls")
        assert controls.status_code == 200
        assert controls.json()["tokens_consumed"] == 42

        events_before_replay = sse_payloads(
            client.get(f"/api/v1/tasks/{task_id}/events")
        )
        external_completion_events = [
            event
            for event in events_before_replay
            if event["event_type"] == "runtime.execution_completed"
            and event["payload"].get("runtime_event_id") == "fake-runtime-event-1"
        ]
        assert len(external_completion_events) == 1

        replayed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=backend["execution_id"],
            runtime_event_id="fake-runtime-event-1",
            summary="backend completed after external event",
        )
        assert replayed.status == "completed"
        events_after_replay = sse_payloads(
            client.get(f"/api/v1/tasks/{task_id}/events")
        )
        assert len(events_after_replay) == len(events_before_replay)

        with pytest.raises(
            ValueError,
            match="already completed with another Runtime event",
        ):
            app.state.task_orchestrator.complete_runtime_execution(
                execution_id=backend["execution_id"],
                runtime_event_id="fake-runtime-event-2",
                summary="conflicting completion",
            )

        with SqliteSaver.from_conn_string(
            str(settings.langgraph_checkpoint_path)
        ) as saver:
            graph = build_task_graph(
                lambda work: None,
                lambda task_id, results: None,
            ).compile(checkpointer=saver)
            assert not graph.get_state(config).interrupts


def test_runtime_concurrency_defers_and_wakes_one_graph_branch(tmp_path) -> None:
    settings = task_settings(tmp_path)
    settings.runtime_max_concurrent_executions = 1
    adapter = FakeRuntimeAdapter(wait_once_role_keys={"backend", "test"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "concurrency-control"},
            json={"request": "Run two bounded assignments with one Runtime slot."},
        )

        assert submitted.status_code == 201
        payload = submitted.json()
        by_role = {
            item["agent_role_key"]: item for item in payload["assignments"]
        }
        backend = by_role["backend"]
        test_assignment = by_role["test"]
        assert backend["runtime_execution"]["wait_reason"] is None
        assert test_assignment["runtime_execution"]["wait_reason"] == (
            "concurrency_limit"
        )
        assert adapter.call_count(backend["execution_id"]) == 1
        assert adapter.call_count(test_assignment["execution_id"]) == 0

        app.state.task_orchestrator.complete_runtime_execution(
            execution_id=backend["execution_id"],
            runtime_event_id="concurrency-backend-completed",
            summary="backend completed and released the slot",
        )
        after_release = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        after_by_role = {
            item["agent_role_key"]: item for item in after_release["assignments"]
        }
        assert after_by_role["test"]["runtime_execution"]["wait_reason"] is None
        assert adapter.call_count(test_assignment["execution_id"]) == 1

        completed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=test_assignment["execution_id"],
            runtime_event_id="concurrency-test-completed",
            summary="test completed after capacity became available",
        )
        assert completed.status == "completed"
        event_types = {
            event["event_type"]
            for event in sse_payloads(
                client.get(f"/api/v1/tasks/{payload['task_id']}/events")
            )
        }
        assert "runtime.execution_deferred" in event_types
        assert "runtime.execution_capacity_available" in event_types


def test_product_token_budget_rejects_work_before_runtime_submission(tmp_path) -> None:
    settings = task_settings(tmp_path)
    settings.runtime_token_budget_limit = 32
    settings.runtime_token_reservation_per_execution = 16
    adapter = FakeRuntimeAdapter()
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        rejected = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "product-budget"},
            json={"request": "Respect the product-owned token budget."},
        )

        assert rejected.status_code == 409
        assert rejected.json()["code"] == "RUNTIME_BUDGET_EXCEEDED"
        with app.state.database.session() as session:
            task_id = session.scalar(select(Task)).task_id
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        assert task["status"] == "failed"
        by_role = {item["agent_role_key"]: item for item in task["assignments"]}
        assert adapter.call_count(by_role["backend"]["execution_id"]) == 1
        assert adapter.call_count(by_role["test"]["execution_id"]) == 1
        assert adapter.call_count(by_role["lead"]["execution_id"]) == 0

        controls = client.get("/api/v1/runtime/controls").json()
        assert controls["token_budget_limit"] == 32
        assert controls["tokens_reserved"] == 0
        assert controls["tokens_consumed"] == 32
        assert controls["tokens_remaining"] == 0


def test_explicit_provider_limit_is_not_treated_as_available(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(
        capacity=RuntimeCapacity(
            status="limited",
            reason="rate_limit_reached",
            resets_at=2_000_000_000,
        )
    )
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        rejected = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "provider-rate-limit"},
            json={"request": "Do not start work while the Provider is limited."},
        )

        assert rejected.status_code == 429
        assert rejected.json()["code"] == "PROVIDER_RATE_LIMITED"
        assert rejected.json()["details"]["resets_at"] == 2_000_000_000
        with app.state.database.session() as session:
            execution = session.scalar(select(RuntimeExecution))
            task = session.scalar(select(Task))
        assert execution is not None
        assert execution.status == "failed"
        assert task is not None
        assert task.status == "failed"
        assert adapter.call_count(execution.execution_id) == 0

        controls = client.get("/api/v1/runtime/controls").json()
        assert controls["provider_capacity_status"] == "limited"
        assert controls["provider_capacity_reason"] == "rate_limit_reached"


def test_multiple_waiting_runtime_branches_resume_independently(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(wait_once_role_keys={"backend", "test"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "multiple-waiting-branches"},
            json={"request": "Resume two independent Runtime branches."},
        )

        assert submitted.status_code == 201
        payload = submitted.json()
        task_id = payload["task_id"]
        assignment_by_role = {
            item["agent_role_key"]: item for item in payload["assignments"]
        }
        assert payload["status"] == "waiting"
        assert all(
            assignment["runtime_execution"]["status"] == "waiting"
            for assignment in assignment_by_role.values()
        )

        first = assignment_by_role["backend"]
        still_waiting = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=first["execution_id"],
            runtime_event_id="multi-runtime-event-1",
            summary="backend external completion",
        )
        assert still_waiting.status == "waiting"
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "waiting"

        replayed_first = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=first["execution_id"],
            runtime_event_id="multi-runtime-event-1",
            summary="backend external completion",
        )
        assert replayed_first.status == "waiting"

        second = assignment_by_role["test"]
        completed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=second["execution_id"],
            runtime_event_id="multi-runtime-event-2",
            summary="test external completion",
        )
        assert completed.status == "completed"
        final = client.get(f"/api/v1/tasks/{task_id}").json()
        assert final["status"] == "completed"
        assert final["result_summary"] == (
            "The organization lead accepted the specialist deliveries."
        )
        lead = next(
            assignment
            for assignment in final["assignments"]
            if assignment["agent_role_key"] == "lead"
        )
        assert "backend external completion" in lead["instructions"]
        assert "test external completion" in lead["instructions"]
        assert all(
            adapter.call_count(assignment["execution_id"]) == 1
            for assignment in assignment_by_role.values()
        )


def test_waiting_runtime_resumes_after_backend_restart(tmp_path) -> None:
    settings = task_settings(tmp_path)
    initial_adapter = FakeRuntimeAdapter(wait_once_role_keys={"backend"})
    initial_app = create_app(settings, runtime_adapter=initial_adapter)

    with TestClient(initial_app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "restart-waiting-task"},
            json={"request": "Resume this task after restarting the backend."},
        )
        assert submitted.status_code == 201
        payload = submitted.json()
        assert payload["status"] == "waiting"
        task_id = payload["task_id"]
        assignment_by_role = {
            item["agent_role_key"]: item for item in payload["assignments"]
        }
        backend_execution_id = assignment_by_role["backend"]["execution_id"]

    restarted_adapter = FakeRuntimeAdapter()
    restarted_app = create_app(settings, runtime_adapter=restarted_adapter)
    with TestClient(restarted_app) as client:
        login(client)
        completed = restarted_app.state.task_orchestrator.complete_runtime_execution(
            execution_id=backend_execution_id,
            runtime_event_id="restart-runtime-event-1",
            summary="backend completed after process restart",
        )
        final = client.get(f"/api/v1/tasks/{task_id}")

    assert completed.status == "completed"
    assert final.status_code == 200
    assert final.json()["status"] == "completed"
    assert final.json()["result_summary"] == (
        "The organization lead accepted the specialist deliveries."
    )
    lead = next(
        assignment
        for assignment in final.json()["assignments"]
        if assignment["agent_role_key"] == "lead"
    )
    assert "backend completed after process restart" in lead["instructions"]
    assert all(
        restarted_adapter.call_count(assignment["execution_id"]) == 0
        for assignment in assignment_by_role.values()
    )


def test_lead_can_return_task_for_user_directed_revision(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(
        lead_review_decision="needs_revision",
        lead_review_final_summary="The delivery needs a user-directed revision.",
        lead_review_issues=("The test evidence is incomplete.",),
    )
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        task_url = f"/api/v1/organizations/{organization_id}/tasks"
        submitted = client.post(
            task_url,
            headers={"Idempotency-Key": "lead-needs-revision"},
            json={"request": "Require the lead to reject this delivery."},
        )

        assert submitted.status_code == 201
        payload = submitted.json()
        assert payload["status"] == "needs_revision"
        assert payload["result_summary"] == (
            "The delivery needs a user-directed revision."
        )
        assert payload["completed_at"] is None
        assert len(payload["assignments"]) == 3
        lead = next(
            assignment
            for assignment in payload["assignments"]
            if assignment["agent_role_key"] == "lead"
        )
        assert adapter.call_count(lead["execution_id"]) == 1

        events = sse_payloads(
            client.get(f"/api/v1/tasks/{payload['task_id']}/events")
        )
        assert events[-1]["event_type"] == "task.needs_revision"
        assert events[-1]["payload"]["issues"] == [
            "The test evidence is incomplete."
        ]

        replay = client.post(
            task_url,
            headers={"Idempotency-Key": "lead-needs-revision"},
            json={"request": "Require the lead to reject this delivery."},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "needs_revision"
        assert adapter.call_count(lead["execution_id"]) == 1


def test_waiting_lead_review_resumes_without_replaying_specialists(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(wait_once_role_keys={"lead"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "waiting-lead-review"},
            json={"request": "Wait for the organization lead review."},
        )

        assert submitted.status_code == 201
        payload = submitted.json()
        assert payload["status"] == "waiting"
        assignment_by_role = {
            assignment["agent_role_key"]: assignment
            for assignment in payload["assignments"]
        }
        assert set(assignment_by_role) == {"backend", "lead", "test"}
        assert assignment_by_role["backend"]["status"] == "completed"
        assert assignment_by_role["test"]["status"] == "completed"
        lead = assignment_by_role["lead"]
        assert lead["status"] == "waiting"
        specialist_call_counts = {
            role_key: adapter.call_count(assignment_by_role[role_key]["execution_id"])
            for role_key in ("backend", "test")
        }

        completed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=lead["execution_id"],
            runtime_event_id="lead-review-completed-1",
            summary=json.dumps(
                {
                    "decision": "accepted",
                    "final_summary": "The lead accepted the completed work.",
                    "issues": [],
                }
            ),
        )

        assert completed.status == "completed"
        final = client.get(f"/api/v1/tasks/{payload['task_id']}").json()
        assert final["result_summary"] == "The lead accepted the completed work."
        assert adapter.call_count(lead["execution_id"]) == 1
        assert all(
            adapter.call_count(assignment_by_role[role_key]["execution_id"])
            == specialist_call_counts[role_key]
            for role_key in ("backend", "test")
        )
