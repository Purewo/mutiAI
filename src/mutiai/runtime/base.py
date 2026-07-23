"""Runtime-neutral execution contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    runtime_job_id: str
    summary: str


class AgentRuntimeAdapter(Protocol):
    provider: str

    def execute(
        self,
        *,
        execution_id: str,
        role_key: str,
        instructions: str,
    ) -> RuntimeResult: ...
