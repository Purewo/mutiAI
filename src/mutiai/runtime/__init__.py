"""External Agent Runtime adapter boundary."""

from mutiai.runtime.base import AgentRuntimeAdapter, RuntimeResult
from mutiai.runtime.fake import FakeRuntimeAdapter
from mutiai.runtime.workspaces import WorkspaceBoundaryError, WorkspaceManager

__all__ = [
    "AgentRuntimeAdapter",
    "FakeRuntimeAdapter",
    "RuntimeResult",
    "WorkspaceBoundaryError",
    "WorkspaceManager",
]
