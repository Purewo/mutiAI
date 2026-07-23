"""Canonical path enforcement for product-managed Runtime workspaces."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


LOCAL_CONTROL_PLANE_ROOT = Path(r"G:\AI\AI_private\Codex_projects")


class WorkspaceBoundaryError(ValueError):
    """Raised when a Runtime path escapes the managed workspace boundary."""


class WorkspaceManager:
    """Validate Runtime paths without provisioning or deleting directories."""

    def __init__(
        self,
        root: str | Path,
        *,
        protected_roots: Iterable[str | Path] | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve(strict=False)
        configured_protected_roots = protected_roots
        if configured_protected_roots is None:
            configured_protected_roots = (
                (LOCAL_CONTROL_PLANE_ROOT,) if os.name == "nt" else ()
            )
        self._protected_roots = tuple(
            Path(path).expanduser().resolve(strict=False)
            for path in configured_protected_roots
        )
        for protected_root in self._protected_roots:
            if self._paths_overlap(self._root, protected_root):
                raise WorkspaceBoundaryError(
                    "managed Runtime root overlaps a protected source root"
                )

    @property
    def root(self) -> Path:
        """Return the canonical managed Runtime root without creating it."""

        return self._root

    def canonicalize(
        self,
        candidate: str | Path,
        *,
        must_exist: bool = True,
    ) -> Path:
        """Return a canonical strict descendant or reject the candidate path."""

        raw_candidate = Path(candidate).expanduser()
        if not raw_candidate.is_absolute():
            raw_candidate = self._root / raw_candidate
        try:
            canonical = raw_candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceBoundaryError(
                "Runtime workspace path cannot be resolved"
            ) from exc

        if canonical == self._root or not canonical.is_relative_to(self._root):
            raise WorkspaceBoundaryError(
                "Runtime workspace must be a strict descendant of the managed root"
            )
        if any(
            self._paths_overlap(canonical, protected_root)
            for protected_root in self._protected_roots
        ):
            raise WorkspaceBoundaryError(
                "Runtime workspace overlaps a protected source root"
            )
        return canonical

    @staticmethod
    def _paths_overlap(left: Path, right: Path) -> bool:
        return left.is_relative_to(right) or right.is_relative_to(left)
