"""Capture contract-valid Runtime feasibility responses from the real API."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from mutiai.api.errors import ErrorEnvelope
from mutiai.api.schemas.feasibility import FeasibilityCheckResponse
from mutiai.api.schemas.organizations import OrganizationVersionResponse
from mutiai.api.schemas.runtime_bindings import RuntimeBindingResponse
from mutiai.api.schemas.tasks import TaskResponse
from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import FakeRuntimeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "contracts" / "fixtures" / "feasibility"


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


def role_spec(binding_key: str, responsibility: str) -> dict:
    return {
        "schema_version": "1.0",
        "name": "Runtime Feasibility Demo",
        "description": "Captured feasibility contract fixture",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": "Plan, delegate, review, and summarize",
                "is_lead": True,
                "reports_to": None,
                "runtime_binding_key": binding_key,
            },
            {
                "role_key": "specialist",
                "name": "Specialist",
                "responsibility": responsibility,
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": binding_key,
            },
        ],
    }


def create_proposal(client: TestClient, spec: dict) -> dict:
    response = client.post(
        "/api/v1/organizations/proposals",
        json={"source_request": "Create a captured fixture", "spec": spec},
    )
    response.raise_for_status()
    payload = response.json()
    OrganizationVersionResponse.model_validate(payload)
    return payload


def publish_proposal(client: TestClient, proposal: dict) -> str:
    base = (
        f"/api/v1/organizations/{proposal['organization_id']}/versions/"
        f"{proposal['spec_version_id']}"
    )
    client.post(base + "/confirm").raise_for_status()
    client.post(base + "/publish").raise_for_status()
    return proposal["organization_id"]


def capture(client: TestClient) -> None:
    linux_binding = client.put(
        "/api/v1/runtime/bindings/linux-standard",
        json={
            "provider": "fake",
            "model": "fixture-model",
            "reasoning_effort": "medium",
            "security_mode": "demo_full_access",
            "capability_profile": {
                "os_family": "linux",
                "headless": True,
                "cpu_capacity_class": "standard",
                "gpu_available": False,
                "network_access": True,
            },
        },
    )
    linux_binding.raise_for_status()
    RuntimeBindingResponse.model_validate(linux_binding.json())
    write_fixture("runtime-binding-linux-standard.json", linux_binding.json())

    blocked_proposal = create_proposal(
        client,
        role_spec(
            "linux-standard",
            "Use PowerShell and the Windows registry to configure the host",
        ),
    )
    blocked_base = (
        f"/api/v1/organizations/{blocked_proposal['organization_id']}/versions/"
        f"{blocked_proposal['spec_version_id']}"
    )
    checks = client.get(
        blocked_base + "/feasibility-checks",
        headers={"Accept-Language": "zh-CN"},
    )
    checks.raise_for_status()
    for item in checks.json():
        FeasibilityCheckResponse.model_validate(item)
    write_fixture("organization-blocked-check.zh-CN.json", checks.json()[-1])

    blocked_confirmation = client.post(
        blocked_base + "/confirm",
        headers={"Accept-Language": "zh-CN"},
    )
    assert blocked_confirmation.status_code == 409
    ErrorEnvelope.model_validate(blocked_confirmation.json())
    write_fixture(
        "organization-blocked-error.zh-CN.json",
        blocked_confirmation.json(),
    )

    unknown_binding = client.put(
        "/api/v1/runtime/bindings/linux-unknown-gpu",
        json={
            "provider": "fake",
            "model": "fixture-model",
            "reasoning_effort": "medium",
            "security_mode": "demo_full_access",
            "capability_profile": {
                "os_family": "linux",
                "headless": True,
                "cpu_capacity_class": "standard",
                "gpu_available": None,
                "network_access": True,
            },
        },
    )
    unknown_binding.raise_for_status()
    unknown_organization = publish_proposal(
        client,
        create_proposal(
            client,
            role_spec("linux-unknown-gpu", "Complete bounded specialist work"),
        ),
    )
    blocked_task = client.post(
        f"/api/v1/organizations/{unknown_organization}/tasks",
        headers={
            "Accept-Language": "zh-CN",
            "Idempotency-Key": "fixture-unknown-gpu",
        },
        json={
            "request": "Run a bounded accelerator workload",
            "capability_requirements": {"requires_gpu": True},
        },
    )
    assert blocked_task.status_code == 409
    ErrorEnvelope.model_validate(blocked_task.json())
    write_fixture("task-capability-unknown-error.zh-CN.json", blocked_task.json())
    check_id = blocked_task.json()["details"]["feasibility_check_id"]
    unknown_check = client.get(
        f"/api/v1/feasibility-checks/{check_id}",
        headers={"Accept-Language": "zh-CN"},
    )
    unknown_check.raise_for_status()
    FeasibilityCheckResponse.model_validate(unknown_check.json())
    write_fixture("task-capability-unknown-check.zh-CN.json", unknown_check.json())

    feasible_organization = publish_proposal(
        client,
        create_proposal(
            client,
            role_spec("linux-standard", "Write and review text documents"),
        ),
    )
    feasible_task = client.post(
        f"/api/v1/organizations/{feasible_organization}/tasks",
        headers={"Idempotency-Key": "fixture-feasible-task"},
        json={"request": "Write a concise text summary"},
    )
    feasible_task.raise_for_status()
    TaskResponse.model_validate(feasible_task.json())
    task_checks = client.get(
        f"/api/v1/tasks/{feasible_task.json()['task_id']}/feasibility-checks",
        headers={"Accept-Language": "zh-CN"},
    )
    task_checks.raise_for_status()
    for item in task_checks.json():
        FeasibilityCheckResponse.model_validate(item)
    write_fixture("task-feasible-checks.zh-CN.json", task_checks.json())


def main() -> None:
    with TemporaryDirectory(prefix="mutiai-feasibility-fixtures-") as temp_dir:
        root = Path(temp_dir)
        app = create_app(
            Settings(
                app_env="test",
                database_url=f"sqlite+pysqlite:///{root / 'fixtures.db'}",
                langgraph_checkpoint_path=root / "checkpoints.db",
                runtime_workspace_root=root / "runtime-workspaces",
                bootstrap_admin_enabled=True,
                bootstrap_admin_username="admin",
                bootstrap_admin_password="123456",
            ),
            runtime_adapter=FakeRuntimeAdapter(),
        )
        with TestClient(app) as client:
            login(client)
            capture(client)

    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    write_fixture(
        "SNAPSHOT.json",
        {
            "source_commit": source_commit,
            "generator": "scripts/export_feasibility_fixtures.py",
            "transport": "FastAPI TestClient",
            "locale": "zh-CN",
        },
    )


if __name__ == "__main__":
    main()
