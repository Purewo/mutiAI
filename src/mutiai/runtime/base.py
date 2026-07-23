"""Runtime-neutral execution contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    status: Literal["completed", "waiting"]
    runtime_job_id: str
    summary: str | None = None


class AgentRuntimeAdapter(Protocol):
    provider: str

    def execute(
        self,
        *,
        execution_id: str,
        role_key: str,
        instructions: str,
    ) -> RuntimeResult: ...
