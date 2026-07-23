"""External Agent Runtime adapter boundary."""

from mutiai.runtime.base import AgentRuntimeAdapter, RuntimeResult
from mutiai.runtime.fake import FakeRuntimeAdapter

__all__ = ["AgentRuntimeAdapter", "FakeRuntimeAdapter", "RuntimeResult"]
