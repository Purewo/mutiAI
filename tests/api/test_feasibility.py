from fastapi.testclient import TestClient

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import FakeRuntimeAdapter


def feasibility_app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'feasibility.db'}",
        langgraph_checkpoint_path=tmp_path / "checkpoints.db",
        runtime_workspace_root=tmp_path / "runtime-workspaces",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
    )
    return create_app(settings, runtime_adapter=FakeRuntimeAdapter())


def login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 200


def put_binding(
    client: TestClient,
    binding_key: str,
    *,
    os_family: str,
    gpu_available: bool | None,
) -> dict:
    response = client.put(
        f"/api/v1/runtime/bindings/{binding_key}",
        json={
            "provider": "fake",
            "model": "test-model",
            "reasoning_effort": "medium",
            "security_mode": "demo_full_access",
            "capability_profile": {
                "os_family": os_family,
                "headless": True,
                "cpu_capacity_class": "standard",
                "gpu_available": gpu_available,
                "network_access": True,
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def organization_spec(
    binding_key: str = "codex-local-default",
    *,
    specialist_responsibility: str = "Complete bounded specialist work",
) -> dict:
    return {
        "schema_version": "1.0",
        "name": "Feasibility Team",
        "description": "Exercises Runtime feasibility gates",
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
                "responsibility": specialist_responsibility,
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": binding_key,
            },
        ],
    }


def propose(client: TestClient, spec: dict) -> dict:
    response = client.post(
        "/api/v1/organizations/proposals",
        json={"source_request": "Create this organization", "spec": spec},
    )
    assert response.status_code == 201
    return response.json()


def publish(client: TestClient, spec: dict) -> str:
    proposal = propose(client, spec)
    base = (
        f"/api/v1/organizations/{proposal['organization_id']}/versions/"
        f"{proposal['spec_version_id']}"
    )
    assert client.post(base + "/confirm").status_code == 200
    assert client.post(base + "/publish").status_code == 200
    return proposal["organization_id"]


def test_windows_role_is_blocked_by_linux_binding_and_returns_alternative(
    tmp_path,
) -> None:
    app = feasibility_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        binding = put_binding(
            client,
            "linux-standard",
            os_family="linux",
            gpu_available=False,
        )
        assert binding["capability_profile"]["revision"] == 1
        proposal = propose(
            client,
            organization_spec(
                "linux-standard",
                specialist_responsibility=(
                    "Use PowerShell and the Windows registry to configure the host"
                ),
            ),
        )
        checks_url = (
            f"/api/v1/organizations/{proposal['organization_id']}/versions/"
            f"{proposal['spec_version_id']}/feasibility-checks"
        )
        checks = client.get(
            checks_url,
            headers={"Accept-Language": "zh-CN"},
        )
        assert checks.status_code == 200
        assert checks.json()[-1]["outcome"] == "blocked"
        finding = next(
            item
            for item in checks.json()[-1]["findings"]
            if item["reason_code"] == "OS_CAPABILITY_MISMATCH"
        )
        assert finding["role_key"] == "specialist"
        assert finding["message"] == "任务要求的操作系统与 Runtime 不匹配。"
        assert "use_linux_native_tool" in finding["alternative_codes"]

        confirmation = client.post(
            checks_url.removesuffix("/feasibility-checks") + "/confirm",
            headers={"Accept-Language": "zh-CN"},
        )
        assert confirmation.status_code == 409
        assert confirmation.json()["code"] == "FEASIBILITY_BLOCKED"
        assert confirmation.json()["message"] == (
            "运行环境不满足当前组织或任务要求。"
        )


def test_gpu_unknown_fails_closed_before_task_creation(tmp_path) -> None:
    app = feasibility_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        put_binding(
            client,
            "unknown-gpu",
            os_family="linux",
            gpu_available=None,
        )
        organization_id = publish(
            client,
            organization_spec("unknown-gpu"),
        )
        blocked = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={
                "Idempotency-Key": "gpu-unknown-task",
                "Accept-Language": "zh-CN",
            },
            json={
                "request": "Run a bounded accelerator workload",
                "capability_requirements": {"requires_gpu": True},
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "FEASIBILITY_CAPABILITY_UNKNOWN"
        check_id = blocked.json()["details"]["feasibility_check_id"]
        check = client.get(
            f"/api/v1/feasibility-checks/{check_id}",
            headers={"Accept-Language": "zh-CN"},
        )
        assert check.status_code == 200
        assert check.json()["outcome"] == "capability_unknown"
        assert {
            item["reason_code"] for item in check.json()["findings"]
        } == {"GPU_CAPABILITY_UNKNOWN"}
        assert client.get("/api/v1/organizations").status_code == 200


def test_video_editing_role_is_blocked_by_standard_cpu_profile(tmp_path) -> None:
    app = feasibility_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        proposal = propose(
            client,
            organization_spec(
                specialist_responsibility="Edit and render long videos locally"
            ),
        )
        base = (
            f"/api/v1/organizations/{proposal['organization_id']}/versions/"
            f"{proposal['spec_version_id']}"
        )
        confirmation = client.post(base + "/confirm")
        assert confirmation.status_code == 409
        check_id = confirmation.json()["details"]["feasibility_check_id"]
        check = client.get(f"/api/v1/feasibility-checks/{check_id}")
        assert check.status_code == 200
        assert any(
            finding["reason_code"] == "CPU_CAPACITY_INSUFFICIENT"
            for finding in check.json()["findings"]
        )


def test_feasible_task_persists_runtime_start_checks(tmp_path) -> None:
    app = feasibility_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        organization_id = publish(client, organization_spec())
        submitted = client.post(
            f"/api/v1/organizations/{organization_id}/tasks",
            headers={"Idempotency-Key": "feasible-text-task"},
            json={"request": "Write a concise text summary"},
        )
        assert submitted.status_code == 201
        task = submitted.json()
        assert task["capability_requirements"]["requires_gpu"] is False
        checks = client.get(
            f"/api/v1/tasks/{task['task_id']}/feasibility-checks"
        )
        assert checks.status_code == 200
        assert checks.json()
        assert {check["outcome"] for check in checks.json()} == {"feasible"}
        assert {check["phase"] for check in checks.json()} == {
            "task_submission",
            "runtime_start",
        }
