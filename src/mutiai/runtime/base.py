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


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryRequest:
    """Durable Runtime identities needed to reattach after owner restart."""

    execution_id: str
    runtime_job_id: str | None
    thread_id: str
    turn_id: str
    workspace_id: str
    workspace_path: str


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

    def recover(self, request: RuntimeRecoveryRequest) -> bool: ...

    def cancel(self, execution_id: str) -> bool:
        """Request cancellation and report whether a live Runtime accepted it."""

        ...
