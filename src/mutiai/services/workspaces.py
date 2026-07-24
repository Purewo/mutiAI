"""Idempotent product Workspace records and directory provisioning."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.models import Workspace, WorkspaceStatus
from mutiai.models.base import utc_now
from mutiai.runtime import WorkspaceBoundaryError, WorkspaceManager


@dataclass(frozen=True, slots=True)
class CodexHomeBootstrapResult:
    """Files copied or intentionally retained in an isolated Codex home."""

    codex_home: Path
    copied: tuple[str, ...]
    skipped: tuple[str, ...]


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

    def bootstrap_codex_home(
        self,
        source_home: str | Path,
        *,
        replace: bool = False,
    ) -> CodexHomeBootstrapResult:
        """Copy provider configuration without adopting interactive sessions."""

        source = Path(source_home).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise WorkspaceBoundaryError("Codex configuration source must be a directory")
        codex_home = self.ensure_codex_home()
        if source == codex_home:
            raise WorkspaceBoundaryError(
                "Codex configuration source and managed home must differ"
            )

        copied: list[str] = []
        skipped: list[str] = []
        with self._lock:
            for name in ("config.toml", "auth.json"):
                source_file = source / name
                if not source_file.is_file():
                    raise WorkspaceBoundaryError(
                        f"Codex configuration source has no {name}"
                    )
                target_file = codex_home / name
                if target_file.exists() and not replace:
                    skipped.append(name)
                    continue

                descriptor, temporary_name = tempfile.mkstemp(
                    dir=codex_home,
                    prefix=f".{name}.",
                    suffix=".tmp",
                )
                os.close(descriptor)
                temporary_file = Path(temporary_name)
                try:
                    shutil.copy2(source_file, temporary_file)
                    os.replace(temporary_file, target_file)
                finally:
                    temporary_file.unlink(missing_ok=True)
                copied.append(name)

        return CodexHomeBootstrapResult(
            codex_home=codex_home,
            copied=tuple(copied),
            skipped=tuple(skipped),
        )

    @staticmethod
    def deterministic_workspace_id(
        *,
        owner_user_id: str,
        organization_id: str,
        agent_role_key: str,
        runtime_provider: str,
    ) -> str:
        identity = (
            f"mutiai:workspace:{owner_user_id}:{organization_id}:"
            f"{agent_role_key}:{runtime_provider}"
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
