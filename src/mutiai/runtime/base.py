"""Runtime-neutral execution contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class RuntimeTokenUsage:
    """Normalized token counts reported by an external Runtime."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeCapacity:
    """Provider capacity signal; ``unknown`` is an explicit valid state."""

    status: Literal["available", "limited", "unknown"]
    reason: str | None = None
    resets_at: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeExecutionConfig:
    """Resolved product policy snapshot supplied to one Runtime execution."""

    binding_key: str
    model: str | None
    reasoning_effort: str | None
    security_mode: Literal["demo_full_access", "workspace_restricted"]
    approval_policy: Literal["never", "on-request"]
    sandbox_mode: Literal["danger-full-access", "workspace-write"]
    network_access: bool


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    status: Literal["completed", "waiting"]
    runtime_job_id: str
    summary: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    workspace_id: str | None = None
    last_event_position: str | None = None
    usage: RuntimeTokenUsage | None = None
    context_compactions: int = 0
    actual_model: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryRequest:
    """Durable Runtime identities needed to reattach after owner restart."""

    execution_id: str
    runtime_job_id: str | None
    thread_id: str
    turn_id: str
    workspace_id: str
    workspace_path: str
    runtime_config: RuntimeExecutionConfig | None = None


class AgentRuntimeAdapter(Protocol):
    provider: str

    def capacity(self) -> RuntimeCapacity: ...

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
        runtime_config: RuntimeExecutionConfig | None = None,
    ) -> RuntimeResult: ...

    def recover(self, request: RuntimeRecoveryRequest) -> bool: ...

    def cancel(self, execution_id: str) -> bool:
        """Request cancellation and report whether a live Runtime accepted it."""

        ...
