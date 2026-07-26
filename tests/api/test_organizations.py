from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.models import Organization, OrganizationSpecVersion


def organization_spec(name: str = "Product Team") -> dict:
    return {
        "schema_version": "1.0",
        "name": name,
        "description": f"{name} description",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": "Plan, delegate, review, and summarize",
                "is_lead": True,
                "reports_to": None,
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "backend",
                "name": "Backend Developer",
                "responsibility": "Implement backend work",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "test",
                "name": "Test Engineer",
                "responsibility": "Verify delivered work",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


def organization_app(tmp_path):
    runtime_root = tmp_path / "runtime-workspaces"
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'organizations.db'}",
        runtime_workspace_root=runtime_root,
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="123456",
        assistant_runtime_provider="inherit",
    )
    return create_app(settings), runtime_root


def login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 200


def create_proposal(
    client: TestClient,
    *,
    spec: dict,
    organization_id: str | None = None,
) -> dict:
    response = client.post(
        "/api/v1/organizations/proposals",
        json={
            "organization_id": organization_id,
            "source_request": "Create the requested organization",
            "spec": spec,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_organization_version_lifecycle_and_lazy_runtime(tmp_path) -> None:
    app, runtime_root = organization_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/v1/organizations").status_code == 401
        login(client)

        first = create_proposal(client, spec=organization_spec("Team V1"))
        organization_id = first["organization_id"]
        first_version_id = first["spec_version_id"]
        assert first["status"] == "proposal"
        assert first["version_number"] == 1
        assert not Path(runtime_root).exists()

        detail = client.get(f"/api/v1/organizations/{organization_id}")
        assert detail.status_code == 200
        assert detail.json()["current_published_version_id"] is None
        assert detail.json()["current_published_spec"] is None

        premature = client.post(
            f"/api/v1/organizations/{organization_id}/versions/"
            f"{first_version_id}/publish"
        )
        assert premature.status_code == 409
        assert premature.json()["code"] == "ORGANIZATION_VERSION_STATE_CONFLICT"

        confirm = client.post(
            f"/api/v1/organizations/{organization_id}/versions/"
            f"{first_version_id}/confirm"
        )
        assert confirm.status_code == 200
        assert confirm.json()["status"] == "confirmed"
        assert (
            client.post(
                f"/api/v1/organizations/{organization_id}/versions/"
                f"{first_version_id}/confirm"
            ).status_code
            == 200
        )

        publish = client.post(
            f"/api/v1/organizations/{organization_id}/versions/"
            f"{first_version_id}/publish"
        )
        assert publish.status_code == 200
        assert publish.json()["status"] == "published"
        assert (
            client.post(
                f"/api/v1/organizations/{organization_id}/versions/"
                f"{first_version_id}/publish"
            ).status_code
            == 200
        )

        second = create_proposal(
            client,
            organization_id=organization_id,
            spec=organization_spec("Team V2 ignored"),
        )
        third = create_proposal(
            client,
            organization_id=organization_id,
            spec=organization_spec("Team V3"),
        )
        assert second["version_number"] == 2
        assert third["version_number"] == 3

        client.post(
            f"/api/v1/organizations/{organization_id}/versions/"
            f"{second['spec_version_id']}/confirm"
        )
        stale = client.post(
            f"/api/v1/organizations/{organization_id}/versions/"
            f"{second['spec_version_id']}/publish"
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "ORGANIZATION_VERSION_STALE"

        client.post(
            f"/api/v1/organizations/{organization_id}/versions/"
            f"{third['spec_version_id']}/confirm"
        )
        latest_publish = client.post(
            f"/api/v1/organizations/{organization_id}/versions/"
            f"{third['spec_version_id']}/publish"
        )
        assert latest_publish.status_code == 200

        versions = client.get(f"/api/v1/organizations/{organization_id}/versions")
        assert versions.status_code == 200
        assert [version["status"] for version in versions.json()] == [
            "superseded",
            "confirmed",
            "published",
        ]

        detail = client.get(f"/api/v1/organizations/{organization_id}")
        assert detail.json()["name"] == "Team V3"
        assert detail.json()["current_published_version_id"] == third["spec_version_id"]
        assert detail.json()["current_published_spec"]["name"] == "Team V3"
        assert len(client.get("/api/v1/organizations").json()) == 1
        assert not Path(runtime_root).exists()

    with app.state.database.session() as session:
        organization = session.scalar(select(Organization))
        versions = session.scalars(
            select(OrganizationSpecVersion).order_by(
                OrganizationSpecVersion.version_number
            )
        ).all()

    assert organization is not None
    assert organization.current_published_version_id == versions[2].spec_version_id
    assert len(versions) == 3


def test_invalid_organization_spec_uses_validation_envelope(tmp_path) -> None:
    app, _ = organization_app(tmp_path)

    with TestClient(app) as client:
        login(client)
        invalid_spec = organization_spec()
        invalid_spec["roles"][0]["is_lead"] = False
        response = client.post(
            "/api/v1/organizations/proposals",
            json={"spec": invalid_spec},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"
    assert "exactly one lead" in str(response.json()["details"])


def test_unknown_organization_is_not_disclosed(tmp_path) -> None:
    app, _ = organization_app(tmp_path)

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/v1/organizations/not-owned")

    assert response.status_code == 404
    assert response.json()["code"] == "ORGANIZATION_NOT_FOUND"
