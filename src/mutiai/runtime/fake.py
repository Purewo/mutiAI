"""Deterministic Runtime adapter used by the M1 walking skeleton."""

from __future__ import annotations

import json
from collections import Counter
from threading import Lock
from typing import Any, Literal

from mutiai.runtime.base import RuntimeCapacity, RuntimeRecoveryRequest, RuntimeResult


class FakeRuntimeAdapter:
    provider = "fake"

    def __init__(
        self,
        *,
        fail_once_role_keys: set[str] | None = None,
        wait_once_role_keys: set[str] | None = None,
        lead_review_decision: Literal["accepted", "needs_revision"] = "accepted",
        lead_review_final_summary: str = (
            "The organization lead accepted the specialist deliveries."
        ),
        lead_review_issues: tuple[str, ...] = (),
        capacity: RuntimeCapacity | None = None,
    ) -> None:
        self._lock = Lock()
        self._calls: Counter[str] = Counter()
        self._fail_once_role_keys = fail_once_role_keys or set()
        self._failed_role_keys: set[str] = set()
        self._wait_once_role_keys = wait_once_role_keys or set()
        self._waited_role_keys: set[str] = set()
        self._active_execution_ids: set[str] = set()
        self._cancelled_execution_ids: set[str] = set()
        self._lead_review_decision = lead_review_decision
        self._lead_review_final_summary = lead_review_final_summary
        self._lead_review_issues = lead_review_issues
        self._capacity = capacity or RuntimeCapacity(status="available")

    def capacity(self) -> RuntimeCapacity:
        """Return the deterministic Provider capacity used by tests."""

        return self._capacity

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
                self._active_execution_ids.add(execution_id)
                return RuntimeResult(
                    status="waiting",
                    runtime_job_id=f"fake:{execution_id}",
                )
        summary = f"{role_key} completed its bounded assignment."
        if output_schema is not None and "decision" in output_schema.get(
            "properties", {}
        ):
            summary = json.dumps(
                {
                    "decision": self._lead_review_decision,
                    "final_summary": self._lead_review_final_summary,
                    "issues": list(self._lead_review_issues),
                },
                ensure_ascii=False,
            )
        return RuntimeResult(
            status="completed",
            runtime_job_id=f"fake:{execution_id}",
            summary=summary,
        )

    def recover(self, request: RuntimeRecoveryRequest) -> bool:
        """The in-memory fake Runtime cannot recover across processes."""

        del request
        return False

    def cancel(self, execution_id: str) -> bool:
        """Cancel a waiting fake execution for orchestration tests."""

        with self._lock:
            if execution_id not in self._active_execution_ids:
                return False
            self._active_execution_ids.remove(execution_id)
            self._cancelled_execution_ids.add(execution_id)
            return True

    def was_cancelled(self, execution_id: str) -> bool:
        with self._lock:
            return execution_id in self._cancelled_execution_ids

    def call_count(self, execution_id: str) -> int:
        with self._lock:
            return self._calls[execution_id]
