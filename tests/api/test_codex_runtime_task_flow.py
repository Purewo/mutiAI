import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
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
    return wait_for_task_status(
        client,
        task_id,
        statuses={"completed"},
        timeout=timeout,
    )


def wait_for_task_status(
    client: TestClient,
    task_id: str,
    *,
    statuses: set[str],
    timeout: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout
    last_payload = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        last_payload = response.json()
        if last_payload["status"] in statuses:
            return last_payload
        time.sleep(0.01)
    raise AssertionError(f"task did not reach {statuses}: {last_payload}")


def wait_for_pending_approval(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout
    last_payload = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}/approvals")
        assert response.status_code == 200
        last_payload = response.json()
        pending = [item for item in last_payload if item["status"] == "pending"]
        if pending:
            assert len(pending) == 1
            return pending[0]
        time.sleep(0.01)
    raise AssertionError(f"task did not request approval: {last_payload}")


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
            task = wait_for_completed_task(client, payload["task_id"])
            assert task["result_summary"] == (
                "The organization lead accepted the specialist deliveries."
            )
            final_assignments = {
                item["agent_role_key"]: item for item in task["assignments"]
            }
            assert set(final_assignments) == {"backend", "lead", "test"}
            assert all(
                app.state.runtime_supervisor.error_for(assignment["execution_id"])
                is None for assignment in final_assignments.values()
            )
            first_thread_by_role = {
                role_key: item["runtime_execution"]["thread_id"]
                for role_key, item in final_assignments.items()
            }
            first_turn_by_role = {
                role_key: item["runtime_execution"]["turn_id"]
                for role_key, item in final_assignments.items()
            }
            first_workspace_by_role = {
                role_key: item["runtime_execution"]["workspace_id"]
                for role_key, item in final_assignments.items()
            }

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
            second_assignments = {
                item["agent_role_key"]: item
                for item in second_task["assignments"]
            }
            assert set(second_assignments) == {"backend", "lead", "test"}
            for role_key, assignment in second_assignments.items():
                runtime = assignment["runtime_execution"]
                assert runtime["thread_id"] == first_thread_by_role[role_key]
                assert runtime["turn_id"] != first_turn_by_role[role_key]
                assert runtime["workspace_id"] == first_workspace_by_role[role_key]
            assert all(
                app.state.runtime_supervisor.error_for(assignment["execution_id"])
                is None
                for assignment in second_assignments.values()
            )

        with app.state.database.session() as session:
            assert session.scalar(select(func.count()).select_from(Workspace)) == 3
            workspaces = session.scalars(select(Workspace)).all()
            assert all(item.status == WorkspaceStatus.READY for item in workspaces)
            assert all(
                Path(item.canonical_path).is_relative_to(manager.root)
                for item in workspaces
            )
            executions = session.scalars(select(RuntimeExecution)).all()
            assert len(executions) == 6
            assert all(item.workspace_id for item in executions)
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ProductEvent)
                    .where(ProductEvent.event_type == "runtime.execution_completed")
                )
                == 6
            )
    finally:
        adapter.close()


def test_codex_terminal_failure_can_retry_same_thread_without_replaying_success(
    tmp_path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'codex-retry.db'}",
        langgraph_checkpoint_path=tmp_path / "retry-checkpoints.db",
        runtime_workspace_root=tmp_path / "managed-retry-runtime",
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
                headers={"Idempotency-Key": "codex-terminal-retry"},
                json={"request": "fail-runtime-once, then complete on retry."},
            )
            assert submitted.status_code == 201
            task_id = submitted.json()["task_id"]
            failed = wait_for_task_status(
                client,
                task_id,
                statuses={"failed"},
            )
            failed_by_role = {
                item["agent_role_key"]: item for item in failed["assignments"]
            }
            assert failed_by_role["backend"]["status"] == "failed"
            failed_backend_runtime = failed_by_role["backend"]["runtime_execution"]
            first_thread_id = failed_backend_runtime["thread_id"]
            first_turn_id = failed_backend_runtime["turn_id"]
            first_workspace_id = failed_backend_runtime["workspace_id"]
            assert first_thread_id and first_turn_id and first_workspace_id

            retried = client.post(f"/api/v1/tasks/{task_id}/retry")
            assert retried.status_code == 200
            assert retried.json()["status"] in {"running", "waiting", "completed"}
            completed = wait_for_completed_task(client, task_id)
            completed_by_role = {
                item["agent_role_key"]: item
                for item in completed["assignments"]
            }
            assert set(completed_by_role) == {"backend", "lead", "test"}
            retried_backend = completed_by_role["backend"]["runtime_execution"]
            assert retried_backend["thread_id"] == first_thread_id
            assert retried_backend["workspace_id"] == first_workspace_id
            assert retried_backend["turn_id"] != first_turn_id
            assert all(
                app.state.runtime_supervisor.error_for(item["execution_id"])
                is None
                for item in completed_by_role.values()
            )

            events_response = client.get(f"/api/v1/tasks/{task_id}/events")
            assert events_response.status_code == 200
            event_types = [
                line.removeprefix("event: ")
                for line in events_response.text.splitlines()
                if line.startswith("event: ")
            ]
            assert event_types.count("runtime.execution_failed") == 1
            assert event_types.count("runtime.execution_retry_requested") == 1
            assert event_types.count("task.retry_requested") == 1
            assert "runtime.execution_watch_failed" not in event_types
            assert event_types[-1] == "task.completed"
    finally:
        adapter.close()


def test_codex_app_server_exit_becomes_explicit_retryable_failure(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'app.db'}",
        langgraph_checkpoint_path=tmp_path / "cp.db",
        runtime_workspace_root=tmp_path / "rt",
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
                headers={"Idempotency-Key": "codex-owner-lost"},
                json={"request": "crash-runtime-once, then retry explicitly."},
            )
            assert submitted.status_code == 201
            task_id = submitted.json()["task_id"]
            failed = wait_for_task_status(client, task_id, statuses={"failed"})
            failed_by_role = {
                item["agent_role_key"]: item for item in failed["assignments"]
            }
            failed_backend = failed_by_role["backend"]["runtime_execution"]
            assert failed_backend["status"] == "failed"

            events_response = client.get(f"/api/v1/tasks/{task_id}/events")
            assert events_response.status_code == 200
            events = [
                json.loads(line.removeprefix("data: "))
                for line in events_response.text.splitlines()
                if line.startswith("data: ")
            ]
            owner_lost = [
                event
                for event in events
                if event["event_type"] == "runtime.execution_failed"
                and event["payload"].get("reason") == "runtime_owner_lost"
            ]
            assert len(owner_lost) == 1
            assert not any(
                event["event_type"] == "runtime.execution_watch_failed"
                for event in events
            )

            retried = client.post(f"/api/v1/tasks/{task_id}/retry")
            assert retried.status_code == 200
            completed = wait_for_completed_task(client, task_id)
            completed_by_role = {
                item["agent_role_key"]: item
                for item in completed["assignments"]
            }
            retried_backend = completed_by_role["backend"]["runtime_execution"]
            assert retried_backend["workspace_id"] == failed_backend["workspace_id"]
            assert retried_backend["thread_id"] == failed_backend["thread_id"]
            assert retried_backend["turn_id"] != failed_backend["turn_id"]
    finally:
        adapter.close()


def test_backend_restart_marks_orphaned_turns_retryable_without_implicit_replay(
    tmp_path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'codex-restart.db'}",
        langgraph_checkpoint_path=tmp_path / "restart-checkpoints.db",
        runtime_workspace_root=tmp_path / "managed-restart-runtime",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
    )

    def build_adapter() -> CodexRuntimeAdapter:
        manager = WorkspaceManager(settings.runtime_workspace_root, protected_roots=())
        provisioner = WorkspaceProvisioner(manager)
        codex_home = provisioner.ensure_codex_home()

        def unexpected_resolver(execution_id: str) -> RuntimeWorkspaceBinding:
            raise AssertionError(f"resolver should not be used for {execution_id}")

        return CodexRuntimeAdapter(
            workspace_manager=manager,
            resolve_workspace=unexpected_resolver,
            codex_home=codex_home,
            command=(sys.executable, str(FAKE_APP_SERVER)),
        )

    first_adapter = build_adapter()
    first_app = create_app(settings, runtime_adapter=first_adapter)
    with TestClient(first_app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "codex-backend-restart"},
            json={"request": "hang-runtime-once until the backend restarts."},
        )
        assert submitted.status_code == 201
        task_id = submitted.json()["task_id"]
        deadline = time.monotonic() + 5
        waiting_by_role = None
        while time.monotonic() < deadline:
            task_response = client.get(f"/api/v1/tasks/{task_id}")
            assert task_response.status_code == 200
            candidate = {
                item["agent_role_key"]: item
                for item in task_response.json()["assignments"]
            }
            if (
                candidate.get("backend", {}).get("status") == "waiting"
                and candidate.get("test", {}).get("status") == "completed"
            ):
                waiting_by_role = candidate
                break
            time.sleep(0.01)
        assert waiting_by_role is not None
        assert set(waiting_by_role) == {"backend", "test"}
        original_runtime_by_role = {
            role_key: item["runtime_execution"]
            for role_key, item in waiting_by_role.items()
        }
        assert original_runtime_by_role["backend"]["status"] == "waiting"
        assert original_runtime_by_role["test"]["status"] == "completed"

    second_adapter = build_adapter()
    second_app = create_app(settings, runtime_adapter=second_adapter)
    try:
        with TestClient(second_app) as client:
            login(client)
            recovered_response = client.get(f"/api/v1/tasks/{task_id}")
            assert recovered_response.status_code == 200
            recovered = recovered_response.json()
            assert recovered["status"] == "failed"
            recovered_by_role = {
                item["agent_role_key"]: item
                for item in recovered["assignments"]
            }
            assert set(recovered_by_role) == {"backend", "test"}
            assert recovered_by_role["backend"]["status"] == "failed"
            assert (
                recovered_by_role["backend"]["runtime_execution"]["status"]
                == "failed"
            )
            assert recovered_by_role["test"]["status"] == "completed"
            assert (
                recovered_by_role["test"]["runtime_execution"]["status"]
                == "completed"
            )

            events_response = client.get(f"/api/v1/tasks/{task_id}/events")
            assert events_response.status_code == 200
            events = [
                json.loads(line.removeprefix("data: "))
                for line in events_response.text.splitlines()
                if line.startswith("data: ")
            ]
            recovery_failures = [
                event
                for event in events
                if event["event_type"] == "runtime.execution_failed"
                and event["payload"].get("reason") == "runtime_owner_lost"
            ]
            assert len(recovery_failures) == 1
            assert recovery_failures[0]["source"] == "runtime.supervisor"
            assert (
                second_app.state.task_orchestrator.recover_orphaned_runtime_executions(
                    is_active=second_adapter.is_active,
                )
                == []
            )

            retried = client.post(f"/api/v1/tasks/{task_id}/retry")
            assert retried.status_code == 200
            completed = wait_for_completed_task(client, task_id)
            completed_by_role = {
                item["agent_role_key"]: item
                for item in completed["assignments"]
            }
            assert set(completed_by_role) == {"backend", "lead", "test"}
            original_backend = original_runtime_by_role["backend"]
            retried_backend = completed_by_role["backend"]["runtime_execution"]
            assert retried_backend["workspace_id"] == original_backend["workspace_id"]
            assert retried_backend["thread_id"] == original_backend["thread_id"]
            assert retried_backend["turn_id"] != original_backend["turn_id"]
            assert (
                completed_by_role["test"]["runtime_execution"]
                == original_runtime_by_role["test"]
            )
            assert all(
                second_app.state.runtime_supervisor.error_for(
                    item["execution_id"]
                )
                is None
                for item in completed_by_role.values()
            )
    finally:
        second_adapter.close()


def test_backend_restart_reattaches_waiting_turn_when_adapter_recovers(
    tmp_path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}",
        langgraph_checkpoint_path=tmp_path / "cp.db",
        runtime_workspace_root=tmp_path / "rt",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
    )

    def build_adapter() -> CodexRuntimeAdapter:
        manager = WorkspaceManager(settings.runtime_workspace_root, protected_roots=())
        provisioner = WorkspaceProvisioner(manager)
        codex_home = provisioner.ensure_codex_home()

        def unexpected_resolver(execution_id: str) -> RuntimeWorkspaceBinding:
            raise AssertionError(f"resolver should not be used for {execution_id}")

        return CodexRuntimeAdapter(
            workspace_manager=manager,
            resolve_workspace=unexpected_resolver,
            codex_home=codex_home,
            command=(sys.executable, str(FAKE_APP_SERVER)),
        )

    first_adapter = build_adapter()
    first_app = create_app(settings, runtime_adapter=first_adapter)
    with TestClient(first_app) as client:
        login(client)
        organization_id = publish_organization(client)
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "codex-backend-reconnect"},
            json={"request": "hang-runtime-once until the backend reconnects."},
        )
        assert submitted.status_code == 201
        task_id = submitted.json()["task_id"]
        waiting_by_role = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            candidate = {
                item["agent_role_key"]: item
                for item in client.get(f"/api/v1/tasks/{task_id}").json()[
                    "assignments"
                ]
            }
            if candidate.get("backend", {}).get("status") == "waiting":
                waiting_by_role = candidate
                break
            time.sleep(0.01)
        assert waiting_by_role is not None
        waiting_execution_id = waiting_by_role["backend"]["execution_id"]

    second_adapter = build_adapter()
    second_app = create_app(settings, runtime_adapter=second_adapter)
    reconnected: list[str] = []
    second_app.state.task_orchestrator.set_runtime_watch(reconnected.append)
    try:
        with patch.object(second_adapter, "recover", return_value=True) as recover, TestClient(
            second_app
        ) as client:
            login(client)
            recovered_response = client.get(f"/api/v1/tasks/{task_id}")
            assert recovered_response.status_code == 200
            assert recovered_response.json()["status"] == "waiting"
            recover.assert_called_once()
            assert reconnected == [waiting_execution_id]
            events_response = client.get(f"/api/v1/tasks/{task_id}/events")
            events = [
                json.loads(line.removeprefix("data: "))
                for line in events_response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert any(
                event["event_type"] == "runtime.execution_reconnected"
                for event in events
            )
            assert not any(
                event["event_type"] == "runtime.execution_failed"
                and event["payload"].get("reason") == "runtime_owner_lost"
                for event in events
            )
    finally:
        second_adapter.close()


@pytest.mark.parametrize(
    ("marker", "decision", "kind", "approval_status", "task_status"),
    [
        (
            "request-command-approval",
            "accept",
            "command_execution",
            "accepted",
            "completed",
        ),
        (
            "request-file-approval",
            "decline",
            "file_change",
            "declined",
            "completed",
        ),
        (
            "request-command-approval",
            "cancel",
            "command_execution",
            "cancelled",
            "failed",
        ),
    ],
)
def test_codex_runtime_approval_decision_resumes_original_turn(
    tmp_path,
    marker: str,
    decision: str,
    kind: str,
    approval_status: str,
    task_status: str,
) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'app.db'}",
        langgraph_checkpoint_path=tmp_path / "cp.db",
        runtime_workspace_root=tmp_path / "rt",
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
                headers={"Idempotency-Key": f"approval-{decision}"},
                json={"request": f"{marker} for the backend assignment."},
            )
            assert submitted.status_code == 201
            task_id = submitted.json()["task_id"]
            approval = wait_for_pending_approval(client, task_id)
            assert approval["kind"] == kind
            assert approval["decision"] is None
            assert approval["decided_by_user_id"] is None
            assert approval["thread_id"]
            assert approval["turn_id"]
            if kind == "command_execution":
                assert approval["command"] == "python -m pytest"
                assert approval["details"]["command_actions"][0]["type"] == "unknown"
            else:
                assert approval["command"] is None
                assert approval["details"]["grant_root"]

            decision_url = (
                f"/api/v1/tasks/{task_id}/approvals/"
                f"{approval['approval_id']}/decision"
            )
            resolved_response = client.post(
                decision_url,
                json={"decision": decision},
            )
            assert resolved_response.status_code == 200
            resolved = resolved_response.json()
            assert resolved["status"] == approval_status
            assert resolved["decision"] == decision
            assert resolved["decided_by_user_id"]
            assert resolved["thread_id"] == approval["thread_id"]
            assert resolved["turn_id"] == approval["turn_id"]

            duplicate = client.post(decision_url, json={"decision": decision})
            assert duplicate.status_code == 200
            conflicting_decision = "decline" if decision == "accept" else "accept"
            conflict = client.post(
                decision_url,
                json={"decision": conflicting_decision},
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "APPROVAL_ALREADY_RESOLVED"

            terminal = wait_for_task_status(
                client,
                task_id,
                statuses={task_status},
            )
            assert terminal["status"] == task_status
            events_response = client.get(f"/api/v1/tasks/{task_id}/events")
            events = [
                json.loads(line.removeprefix("data: "))
                for line in events_response.text.splitlines()
                if line.startswith("data: ")
            ]
            event_types = [event["event_type"] for event in events]
            assert event_types.count("runtime.approval_requested") == 1
            assert event_types.count("runtime.approval_resolved") == 1
            assert "runtime.execution_watch_failed" not in event_types
    finally:
        adapter.close()
