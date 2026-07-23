import json
import sqlite3

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import Assignment, ProductEvent, RuntimeExecution, Task
from mutiai.models.task import TaskStatus
from mutiai.runtime import FakeRuntimeAdapter


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
        assert len(payload["assignments"]) == 2
        assert {item["agent_role_key"] for item in payload["assignments"]} == {
            "backend",
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
        assert len(events) == 16
        assert [event["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )
        assert events[0]["event_type"] == "task.created"
        assert events[-1]["event_type"] == "task.completed"

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
        assert session.scalar(select(func.count()).select_from(Assignment)) == 2
        assert session.scalar(select(func.count()).select_from(RuntimeExecution)) == 2
        assert session.scalar(select(func.count()).select_from(ProductEvent)) == 16

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


def test_failed_parallel_branch_resumes_without_replaying_success(tmp_path) -> None:
    settings = task_settings(tmp_path)
    adapter = FakeRuntimeAdapter(fail_once_role_keys={"backend"})
    app = create_app(settings, runtime_adapter=adapter)

    with TestClient(app, raise_server_exceptions=False) as client:
        login(client)
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

        resumed = client.post(
            task_url,
            headers={"Idempotency-Key": "retry-task"},
            json={"request": "Verify pending writes recovery."},
        )

        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"
        assert len(resumed.json()["assignments"]) == 2
        assert adapter.call_count(execution_by_role["backend"]) == 2
        assert adapter.call_count(execution_by_role["test"]) == 1

        events = sse_payloads(
            client.get(f"/api/v1/tasks/{resumed.json()['task_id']}/events")
        )
        assert "runtime.execution_failed" in {event["event_type"] for event in events}
        assert events[-1]["event_type"] == "task.completed"
        assert [event["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )
