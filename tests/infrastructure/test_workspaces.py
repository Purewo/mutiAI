import os
import subprocess
from pathlib import Path

import pytest

from mutiai.runtime import WorkspaceBoundaryError, WorkspaceManager


def test_workspace_manager_accepts_only_strict_canonical_descendants(tmp_path) -> None:
    root = tmp_path / "managed-runtime-root"
    workspace = root / "users" / "user-1" / "workspaces" / "workspace-1"
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(root, protected_roots=())

    assert manager.root == root.resolve()
    assert manager.canonicalize(workspace) == workspace.resolve()
    assert (
        manager.canonicalize(Path("users") / "user-1" / "workspaces" / "workspace-1")
        == workspace.resolve()
    )

    with pytest.raises(WorkspaceBoundaryError, match="strict descendant"):
        manager.canonicalize(root)
    with pytest.raises(WorkspaceBoundaryError, match="strict descendant"):
        manager.canonicalize(root / ".." / "outside", must_exist=False)


def test_workspace_manager_does_not_create_configured_root(tmp_path) -> None:
    root = tmp_path / "not-created"

    manager = WorkspaceManager(root, protected_roots=())

    assert manager.root == root.resolve()
    assert not root.exists()


def test_workspace_manager_rejects_source_roots_and_unsafe_configuration(
    tmp_path,
) -> None:
    managed_root = tmp_path / "managed"
    source_root = tmp_path / "Codex_projects"
    backend_repo = source_root / "mutiAI"
    frontend_repo = source_root / "mutiAI-aistdio-gemini"
    backend_repo.mkdir(parents=True)
    frontend_repo.mkdir()
    manager = WorkspaceManager(managed_root, protected_roots=(source_root,))

    for source_path in (source_root, backend_repo, frontend_repo):
        with pytest.raises(WorkspaceBoundaryError):
            manager.canonicalize(source_path)

    with pytest.raises(WorkspaceBoundaryError, match="protected source root"):
        WorkspaceManager(source_root, protected_roots=(source_root,))
    with pytest.raises(WorkspaceBoundaryError, match="protected source root"):
        WorkspaceManager(backend_repo, protected_roots=(source_root,))


def test_workspace_manager_rejects_link_or_junction_escape(tmp_path) -> None:
    root = tmp_path / "managed"
    outside = tmp_path / "outside"
    link = root / "escaped"
    root.mkdir()
    outside.mkdir()
    _create_directory_link(link, outside)

    try:
        manager = WorkspaceManager(root, protected_roots=())
        with pytest.raises(WorkspaceBoundaryError, match="strict descendant"):
            manager.canonicalize(link)
        with pytest.raises(WorkspaceBoundaryError, match="strict descendant"):
            manager.canonicalize(link / "future-workspace", must_exist=False)
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows symlink and junction creation are unavailable")
