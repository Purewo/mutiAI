"""Deterministic Runtime adapter used by the M1 walking skeleton."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Literal

from mutiai.runtime.base import (
    RuntimeCapacity,
    RuntimeExecutionConfig,
    RuntimeRecoveryRequest,
    RuntimeResult,
)


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
        planning_plan: dict[str, Any] | None = None,
        capacity: RuntimeCapacity | None = None,
    ) -> None:
        self._lock = Lock()
        self._calls: Counter[str] = Counter()
        self._runtime_configs: dict[str, RuntimeExecutionConfig | None] = {}
        self._fail_once_role_keys = fail_once_role_keys or set()
        self._failed_role_keys: set[str] = set()
        self._wait_once_role_keys = wait_once_role_keys or set()
        self._waited_role_keys: set[str] = set()
        self._active_execution_ids: set[str] = set()
        self._cancelled_execution_ids: set[str] = set()
        self._lead_review_decision = lead_review_decision
        self._lead_review_final_summary = lead_review_final_summary
        self._lead_review_issues = lead_review_issues
        self._planning_plan = planning_plan
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
        runtime_config: RuntimeExecutionConfig | None = None,
        developer_instructions: str | None = None,
        dynamic_tools: list[dict[str, Any]] | None = None,
        thread_config: dict[str, Any] | None = None,
        server_request_handler: Callable[[Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
    ) -> RuntimeResult:
        del developer_instructions, dynamic_tools, thread_config
        del server_request_handler
        del workspace_id, workspace_path, thread_id
        with self._lock:
            self._calls[execution_id] += 1
            self._runtime_configs[execution_id] = runtime_config
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
        elif output_schema is not None and "steps" in output_schema.get(
            "properties", {}
        ):
            planning_plan = self._planning_plan or self._default_planning_plan(
                instructions
            )
            summary = json.dumps(planning_plan, ensure_ascii=False)
        return RuntimeResult(
            status="completed",
            runtime_job_id=f"fake:{execution_id}",
            summary=summary,
        )

    @staticmethod
    def _default_planning_plan(instructions: str) -> dict[str, Any]:
        """Derive a deterministic pure-parallel plan for local API integration."""

        marker = "Published OrganizationSpec:"
        try:
            _, raw_spec = instructions.rsplit(marker, maxsplit=1)
            spec = json.loads(raw_spec.strip())
            roles = spec["roles"]
            lead = next(role for role in roles if role.get("is_lead") is True)
            specialists = sorted(
                (role for role in roles if role.get("is_lead") is not True),
                key=lambda role: role["role_key"],
            )
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError) as exc:
            raise RuntimeError(
                "Fake Runtime could not derive a planning plan from the published "
                "OrganizationSpec"
            ) from exc
        if not specialists:
            raise RuntimeError(
                "Fake Runtime requires at least one specialist role for planning"
            )

        steps: list[dict[str, Any]] = []
        specialist_step_keys: list[str] = []
        specialist_output_keys: list[str] = []
        for index, role in enumerate(specialists, start=1):
            step_key = f"specialist-{index}"
            output_key = f"fake.specialist-{index}.output.v1"
            specialist_step_keys.append(step_key)
            specialist_output_keys.append(output_key)
            steps.append(
                {
                    "step_key": step_key,
                    "role_key": role["role_key"],
                    "step_kind": "specialist",
                    "objective": (
                        "Produce a deterministic fake delivery within this role's "
                        f"responsibility: {role['responsibility']}"
                    ),
                    "acceptance_criteria": (
                        "Return one JSON Artifact for local contract and UI testing."
                    ),
                    "depends_on": [],
                    "input_contracts": [],
                    "output_contracts": [
                        {
                            "contract_key": output_key,
                            "schema_version": "1.0",
                            "media_type": "application/json",
                            "file_name": f"specialist-{index}.json",
                        }
                    ],
                }
            )
        steps.append(
            {
                "step_key": "lead-review",
                "role_key": lead["role_key"],
                "step_kind": "lead_review",
                "objective": "Review every deterministic fake specialist delivery.",
                "acceptance_criteria": (
                    "Return an accepted or needs_revision decision for UI testing."
                ),
                "depends_on": specialist_step_keys,
                "input_contracts": specialist_output_keys,
                "output_contracts": [],
            }
        )
        return {
            "schema_version": "1.0",
            "summary": (
                "Fake Runtime generated a deterministic pure-parallel plan for "
                "local integration testing."
            ),
            "initial_input_contracts": [],
            "steps": steps,
        }

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

    def runtime_config_for(
        self,
        execution_id: str,
    ) -> RuntimeExecutionConfig | None:
        with self._lock:
            return self._runtime_configs.get(execution_id)
