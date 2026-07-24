from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import ProductEvent, RuntimeExecution, Workspace
from mutiai.models.workspace import WorkspaceStatus
from mutiai.runtime import (
    CodexRuntimeAdapter,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
)
from mutiai.services.workspaces import WorkspaceProvisioner


FAKE_APP_SERVER = (
    Path(__file__).resolve().parents[1] / "support" / "fake_codex_app_server.py"
)


def task_spec() -> dict:
    return {
        "schema_version": "1.0",
        "name": "Codex Runtime Team",
        "description": "Exercise the local Codex Runtime boundary",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": "Review and summarize",
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
                "responsibility": "Verify behavior",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


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


def wait_for_completed_task(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout
    last_payload = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        last_payload = response.json()
        if last_payload["status"] == "completed":
            return last_payload
        time.sleep(0.01)
    raise AssertionError(f"task did not complete: {last_payload}")


def test_codex_runtime_submission_persists_workspace_and_resumes_task(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'codex-task.db'}",
        langgraph_checkpoint_path=tmp_path / "checkpoints.db",
        runtime_workspace_root=tmp_path / "managed-runtime",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
    )
    manager = WorkspaceManager(settings.runtime_workspace_root, protected_roots=())
    provisioner = WorkspaceProvisioner(manager)
    codex_home = provisioner.ensure_codex_home()

    def unexpected_resolver(execution_id: str) -> RuntimeWorkspaceBinding:
        raise AssertionError(f"resolver should not be used for {execution_id}")

    adapter = CodexRuntimeAdapter(
        workspace_manager=manager,
        resolve_workspace=unexpected_resolver,
        codex_home=codex_home,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    )
    app = create_app(settings, runtime_adapter=adapter)

    try:
        with TestClient(app) as client:
            login(client)
            organization_id = publish_organization(client)
            submitted = client.post(
                f"/api/v1/organizations/{organization_id}/tasks",
                headers={"Idempotency-Key": "codex-runtime-task"},
                json={"request": "Run the bounded Codex Runtime task."},
            )
            assert submitted.status_code == 201
            payload = submitted.json()
            assert payload["status"] in {"waiting", "completed"}
            assignments = {
                item["agent_role_key"]: item for item in payload["assignments"]
            }
            assert set(assignments) == {"backend", "test"}
            assert all(
                item["runtime_execution"]["status"] in {"waiting", "completed"}
                and item["runtime_execution"]["thread_id"]
                and item["runtime_execution"]["turn_id"]
                and item["runtime_execution"]["workspace_id"]
                for item in assignments.values()
            )
            first_thread_by_role = {
                role_key: item["runtime_execution"]["thread_id"]
                for role_key, item in assignments.items()
            }
            first_turn_by_role = {
                role_key: item["runtime_execution"]["turn_id"]
                for role_key, item in assignments.items()
            }
            first_workspace_by_role = {
                role_key: item["runtime_execution"]["workspace_id"]
                for role_key, item in assignments.items()
            }

            task = wait_for_completed_task(client, payload["task_id"])
            assert (
                task["result_summary"].count("Delivered the bounded assignment.") == 2
            )
            assert all(
                app.state.runtime_supervisor.error_for(assignment["execution_id"])
                is None
                for assignment in assignments.values()
            )

            second_submitted = client.post(
                f"/api/v1/organizations/{organization_id}/tasks",
                headers={"Idempotency-Key": "codex-runtime-task-2"},
                json={"request": "Reuse each role's persistent Codex Thread."},
            )
            assert second_submitted.status_code == 201
            second_payload = second_submitted.json()
            assert second_payload["status"] in {"waiting", "completed"}
            second_assignments = {
                item["agent_role_key"]: item for item in second_payload["assignments"]
            }
            for role_key, assignment in second_assignments.items():
                runtime = assignment["runtime_execution"]
                assert runtime["thread_id"] == first_thread_by_role[role_key]
                assert runtime["turn_id"] != first_turn_by_role[role_key]
                assert runtime["workspace_id"] == first_workspace_by_role[role_key]

            second_task = wait_for_completed_task(
                client,
                second_payload["task_id"],
            )
            assert second_task["status"] == "completed"
            assert all(
                app.state.runtime_supervisor.error_for(assignment["execution_id"])
                is None
                for assignment in second_assignments.values()
            )

        with app.state.database.session() as session:
            assert session.scalar(select(func.count()).select_from(Workspace)) == 2
            workspaces = session.scalars(select(Workspace)).all()
            assert all(item.status == WorkspaceStatus.READY for item in workspaces)
            assert all(
                Path(item.canonical_path).is_relative_to(manager.root)
                for item in workspaces
            )
            executions = session.scalars(select(RuntimeExecution)).all()
            assert len(executions) == 4
            assert all(item.workspace_id for item in executions)
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ProductEvent)
                    .where(ProductEvent.event_type == "runtime.execution_completed")
                )
                == 4
            )
    finally:
        adapter.close()
