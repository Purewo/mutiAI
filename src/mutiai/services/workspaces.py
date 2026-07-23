"""Idempotent product Workspace records and directory provisioning."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.models import Workspace, WorkspaceStatus
from mutiai.models.base import utc_now
from mutiai.runtime import WorkspaceBoundaryError, WorkspaceManager


class WorkspaceProvisioner:
    """Coordinate product records with managed filesystem directories."""

    def __init__(self, manager: WorkspaceManager) -> None:
        self.manager = manager
        self._lock = RLock()

    def ensure_role_workspace(
        self,
        session: Session,
        *,
        owner_user_id: str,
        organization_id: str,
        agent_role_key: str,
        runtime_provider: str = "codex",
    ) -> Workspace:
        """Return one stable ready Workspace for an organization role."""

        with self._lock:
            workspace_id = self.deterministic_workspace_id(
                owner_user_id=owner_user_id,
                organization_id=organization_id,
                agent_role_key=agent_role_key,
                runtime_provider=runtime_provider,
            )
            expected_path = self.manager.canonicalize(
                self._relative_workspace_path(
                    owner_user_id=owner_user_id,
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                ),
                must_exist=False,
            )
            workspace = session.scalar(
                select(Workspace).where(
                    Workspace.organization_id == organization_id,
                    Workspace.agent_role_key == agent_role_key,
                    Workspace.runtime_provider == runtime_provider,
                )
            )
            if workspace is None:
                workspace = Workspace(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    organization_id=organization_id,
                    agent_role_key=agent_role_key,
                    runtime_provider=runtime_provider,
                    canonical_path=str(expected_path),
                    status=WorkspaceStatus.PROVISIONING,
                )
                session.add(workspace)
                session.commit()
            else:
                if workspace.owner_user_id != owner_user_id:
                    raise WorkspaceBoundaryError(
                        "Workspace owner does not match its organization owner"
                    )
                if Path(workspace.canonical_path) != expected_path:
                    raise WorkspaceBoundaryError(
                        "Workspace record path does not match its deterministic path"
                    )
                if workspace.status == WorkspaceStatus.ARCHIVED:
                    raise WorkspaceBoundaryError(
                        "Archived Workspace cannot be provisioned implicitly"
                    )
                workspace.status = WorkspaceStatus.PROVISIONING
                workspace.updated_at = utc_now()
                session.commit()

            try:
                canonical = self.manager.provision(expected_path)
            except WorkspaceBoundaryError:
                workspace.status = WorkspaceStatus.FAILED
                workspace.updated_at = utc_now()
                session.commit()
                raise

            if canonical != expected_path:
                workspace.status = WorkspaceStatus.FAILED
                workspace.updated_at = utc_now()
                session.commit()
                raise WorkspaceBoundaryError(
                    "Provisioned Workspace path changed after canonicalization"
                )

            workspace.canonical_path = str(canonical)
            workspace.status = WorkspaceStatus.READY
            workspace.ready_at = workspace.ready_at or utc_now()
            workspace.updated_at = utc_now()
            session.commit()
            session.refresh(workspace)
            return workspace

    def ensure_codex_home(self) -> Path:
        """Provision the isolated product Codex home on first Runtime use."""

        with self._lock:
            return self.manager.provision(Path("system") / "codex-home")

    @staticmethod
    def deterministic_workspace_id(
        *,
        owner_user_id: str,
        organization_id: str,
        agent_role_key: str,
        runtime_provider: str,
    ) -> str:
        identity = ":".join(
            (
                "mutiai",
                "workspace",
                owner_user_id,
                organization_id,
                agent_role_key,
                runtime_provider,
            )
        )
        return str(uuid5(NAMESPACE_URL, identity))

    @staticmethod
    def _relative_workspace_path(
        *,
        owner_user_id: str,
        organization_id: str,
        workspace_id: str,
    ) -> Path:
        return (
            Path("users")
            / owner_user_id
            / "organizations"
            / organization_id
            / "workspaces"
            / workspace_id
        )
