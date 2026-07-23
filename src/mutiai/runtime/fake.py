"""Deterministic Runtime adapter used by the M1 walking skeleton."""

from __future__ import annotations

from collections import Counter
from threading import Lock

from mutiai.runtime.base import RuntimeResult


class FakeRuntimeAdapter:
    provider = "fake"

    def __init__(
        self,
        *,
        fail_once_role_keys: set[str] | None = None,
        wait_once_role_keys: set[str] | None = None,
    ) -> None:
        self._lock = Lock()
        self._calls: Counter[str] = Counter()
        self._fail_once_role_keys = fail_once_role_keys or set()
        self._failed_role_keys: set[str] = set()
        self._wait_once_role_keys = wait_once_role_keys or set()
        self._waited_role_keys: set[str] = set()

    def execute(
        self,
        *,
        execution_id: str,
        role_key: str,
        instructions: str,
        workspace_id: str | None = None,
        workspace_path: str | None = None,
        thread_id: str | None = None,
    ) -> RuntimeResult:
        del workspace_id, workspace_path, thread_id
        del instructions
        with self._lock:
            self._calls[execution_id] += 1
            if (
                role_key in self._fail_once_role_keys
                and role_key not in self._failed_role_keys
            ):
                self._failed_role_keys.add(role_key)
                raise RuntimeError(f"simulated failure for role '{role_key}'")
            if (
                role_key in self._wait_once_role_keys
                and role_key not in self._waited_role_keys
            ):
                self._waited_role_keys.add(role_key)
                return RuntimeResult(
                    status="waiting",
                    runtime_job_id=f"fake:{execution_id}",
                )
        return RuntimeResult(
            status="completed",
            runtime_job_id=f"fake:{execution_id}",
            summary=f"{role_key} completed its bounded assignment.",
        )

    def call_count(self, execution_id: str) -> int:
        with self._lock:
            return self._calls[execution_id]
