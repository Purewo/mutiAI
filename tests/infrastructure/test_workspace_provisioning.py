from pathlib import Path

import pytest
from sqlalchemy import func, select

from mutiai.config import Settings
from mutiai.db import Database
from mutiai.migrations import upgrade_database
from mutiai.models import Organization, User, Workspace, WorkspaceStatus
from mutiai.runtime import WorkspaceBoundaryError, WorkspaceManager
from mutiai.services.workspaces import WorkspaceProvisioner


def workspace_database(tmp_path) -> tuple[Database, str, str]:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'workspaces.db'}",
        runtime_workspace_root=tmp_path / "managed",
        bootstrap_admin_enabled=False,
    )
    upgrade_database(settings.database_url)
    database = Database(settings)
    with database.session() as session:
        user = User(
            username="workspace-owner",
            password_hash="test-only",
            display_name="Workspace Owner",
        )
        session.add(user)
        session.flush()
        organization = Organization(
            owner_user_id=user.user_id,
            name="Workspace Organization",
            description="",
        )
        session.add(organization)
        session.commit()
        return database, user.user_id, organization.organization_id


def test_role_workspace_provisioning_is_durable_and_idempotent(tmp_path) -> None:
    database, user_id, organization_id = workspace_database(tmp_path)
    manager = WorkspaceManager(tmp_path / "managed", protected_roots=())
    provisioner = WorkspaceProvisioner(manager)

    try:
        with database.session() as session:
            first = provisioner.ensure_role_workspace(
                session,
                owner_user_id=user_id,
                organization_id=organization_id,
                agent_role_key="backend/developer",
            )
            first_id = first.workspace_id
            first_path = Path(first.canonical_path)

        assert first.status == WorkspaceStatus.READY
        assert first_path.is_dir()
        assert first_path.is_relative_to(manager.root)
        assert "backend" not in first_path.parts
        assert not (manager.root / "system" / "codex-home").exists()

        with database.session() as session:
            replayed = provisioner.ensure_role_workspace(
                session,
                owner_user_id=user_id,
                organization_id=organization_id,
                agent_role_key="backend/developer",
            )
            assert replayed.workspace_id == first_id
            assert Path(replayed.canonical_path) == first_path
            assert session.scalar(select(func.count()).select_from(Workspace)) == 1

        first_path.rmdir()
        with database.session() as session:
            recovered = provisioner.ensure_role_workspace(
                session,
                owner_user_id=user_id,
                organization_id=organization_id,
                agent_role_key="backend/developer",
            )
            assert recovered.workspace_id == first_id
            assert Path(recovered.canonical_path).is_dir()

        codex_home = provisioner.ensure_codex_home()
        assert codex_home.is_dir()
        assert codex_home.is_relative_to(manager.root)
    finally:
        database.dispose()


def test_workspace_record_path_cannot_be_redirected_outside_managed_root(
    tmp_path,
) -> None:
    database, user_id, organization_id = workspace_database(tmp_path)
    manager = WorkspaceManager(tmp_path / "managed", protected_roots=())
    provisioner = WorkspaceProvisioner(manager)

    try:
        with database.session() as session:
            workspace = provisioner.ensure_role_workspace(
                session,
                owner_user_id=user_id,
                organization_id=organization_id,
                agent_role_key="backend",
            )
            workspace.canonical_path = str(tmp_path / "outside")
            session.commit()

        with database.session() as session:
            with pytest.raises(
                WorkspaceBoundaryError,
                match="does not match its deterministic path",
            ):
                provisioner.ensure_role_workspace(
                    session,
                    owner_user_id=user_id,
                    organization_id=organization_id,
                    agent_role_key="backend",
                )
    finally:
        database.dispose()
