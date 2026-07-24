"""Runtime-neutral execution contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    status: Literal["completed", "waiting"]
    runtime_job_id: str
    summary: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    workspace_id: str | None = None
    last_event_position: str | None = None


class AgentRuntimeAdapter(Protocol):
    provider: str

    def execute(
        self,
        *,
        execution_id: str,
        role_key: str,
        instructions: str,
        workspace_id: str | None = None,
        workspace_path: str | None = None,
        thread_id: str | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> RuntimeResult: ...
