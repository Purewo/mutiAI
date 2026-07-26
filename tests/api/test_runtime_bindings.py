from fastapi.testclient import TestClient

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import FakeRuntimeAdapter


def binding_app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'bindings.db'}",
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


def test_runtime_binding_list_lazily_creates_default_and_upsert_is_idempotent(
    tmp_path,
) -> None:
    app = binding_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        listed = client.get("/api/v1/runtime/bindings")
        assert listed.status_code == 200
        assert listed.json()[0]["binding_key"] == "codex-local-default"
        assert listed.json()[0]["provider"] == "fake"
        assert listed.json()[0]["security_mode"] == "demo_full_access"

        payload = {
            "provider": "fake",
            "model": "backend-model-test",
            "reasoning_effort": "high",
            "security_mode": "workspace_restricted",
        }
        created = client.put("/api/v1/runtime/bindings/backend", json=payload)
        assert created.status_code == 200
        first = created.json()
        assert first["binding_key"] == "backend"
        assert first["model"] == "backend-model-test"
        assert first["security_mode"] == "workspace_restricted"

        updated = client.put(
            "/api/v1/runtime/bindings/backend",
            json={**payload, "reasoning_effort": "medium"},
        )
        assert updated.status_code == 200
        assert updated.json()["runtime_binding_id"] == first["runtime_binding_id"]
        assert updated.json()["reasoning_effort"] == "medium"

        profiled_payload = {
            **payload,
            "reasoning_effort": "medium",
            "capability_profile": {
                "os_family": "linux",
                "headless": True,
                "cpu_capacity_class": "heavy",
                "memory_mb": 32768,
                "gpu_available": True,
                "gpu_kind": "test-gpu",
                "gpu_memory_mb": 16384,
                "network_access": False,
            },
        }
        profiled = client.put(
            "/api/v1/runtime/bindings/backend",
            json=profiled_payload,
        )
        assert profiled.status_code == 200
        assert profiled.json()["capability_profile"]["revision"] == 2
        assert profiled.json()["capability_profile"]["profile"]["os_family"] == (
            "linux"
        )

        replayed_profile = client.put(
            "/api/v1/runtime/bindings/backend",
            json=profiled_payload,
        )
        assert replayed_profile.status_code == 200
        assert replayed_profile.json()["capability_profile"]["revision"] == 2


def test_runtime_binding_rejects_non_active_provider(tmp_path) -> None:
    app = binding_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        response = client.put(
            "/api/v1/runtime/bindings/claude",
            json={
                "provider": "claude",
                "model": "model-test",
                "reasoning_effort": "high",
                "security_mode": "workspace_restricted",
            },
        )
        assert response.status_code == 409
        assert response.json()["code"] == "RUNTIME_PROVIDER_MISMATCH"


def test_runtime_binding_rejects_full_access_on_non_loopback_service(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        app_host="0.0.0.0",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'restricted-bindings.db'}",
        langgraph_checkpoint_path=tmp_path / "checkpoints.db",
        runtime_workspace_root=tmp_path / "runtime-workspaces",
        runtime_security_mode="workspace_restricted",
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
    )
    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())
    with TestClient(app) as client:
        login(client)
        response = client.put(
            "/api/v1/runtime/bindings/unsafe-demo",
            json={
                "provider": "fake",
                "model": "model-test",
                "reasoning_effort": "medium",
                "security_mode": "demo_full_access",
            },
        )
        assert response.status_code == 409
        assert response.json()["code"] == "RUNTIME_SECURITY_MODE_INVALID"
