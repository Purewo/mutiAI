"""Persist and coordinate product-owned Runtime approval decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from threading import Event, RLock

from sqlalchemy import select

from mutiai.db import Database
from mutiai.models import ApprovalRequest, Assignment, RuntimeExecution, Task
from mutiai.models.approval import (
    ApprovalDecision,
    ApprovalStatus,
)
from mutiai.models.base import utc_now
from mutiai.models.task import RuntimeExecutionStatus, TaskStatus
from mutiai.runtime import CodexApprovalRequest
from mutiai.services.events import append_task_event


class RuntimeApprovalCoordinator:
    """Bridge one App Server request to a durable user decision."""

    def __init__(
        self,
        database: Database,
        *,
        mutation_lock: RLock | None = None,
    ) -> None:
        self.database = database
        self._mutation_lock = mutation_lock or RLock()
        self._waiter_lock = RLock()
        self._waiters: dict[str, Event] = {}
        self._closed = False

    def request_approval(
        self,
        request: CodexApprovalRequest,
    ) -> Mapping[str, str]:
        """Persist an approval request and block only the Runtime worker."""

        runtime_request_id = json.dumps(
            request.request_id,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._waiter_lock:
            if self._closed:
                raise RuntimeError("Runtime approval coordinator is closed")
            with self._mutation_lock, self.database.session() as session:
                execution = session.scalar(
                    select(RuntimeExecution).where(
                        RuntimeExecution.execution_id == request.execution_id
                    )
                )
                if execution is None:
                    raise LookupError(
                        f"execution '{request.execution_id}' does not exist"
                    )
                assignment = session.get(Assignment, execution.assignment_id)
                if assignment is None:
                    raise LookupError(
                        f"assignment '{execution.assignment_id}' does not exist"
                    )
                task = session.get(Task, assignment.task_id)
                if task is None:
                    raise LookupError(f"task '{assignment.task_id}' does not exist")
                if task.status == TaskStatus.CANCELLED:
                    return {"decision": ApprovalDecision.CANCEL}
                if execution.status != RuntimeExecutionStatus.WAITING:
                    raise RuntimeError(
                        f"execution '{execution.execution_id}' is not waiting"
                    )
                if (
                    execution.thread_id != request.thread_id
                    or execution.turn_id != request.turn_id
                ):
                    raise RuntimeError(
                        "approval request does not match the persisted Runtime Turn"
                    )

                approval = session.scalar(
                    select(ApprovalRequest).where(
                        ApprovalRequest.runtime_execution_id
                        == execution.runtime_execution_id,
                        ApprovalRequest.turn_id == request.turn_id,
                        ApprovalRequest.runtime_request_id == runtime_request_id,
                    )
                )
                if approval is None:
                    approval = ApprovalRequest(
                        task_id=task.task_id,
                        assignment_id=assignment.assignment_id,
                        runtime_execution_id=execution.runtime_execution_id,
                        provider=execution.provider,
                        kind=request.kind,
                        status=ApprovalStatus.PENDING,
                        runtime_request_id=runtime_request_id,
                        thread_id=request.thread_id,
                        turn_id=request.turn_id,
                        item_id=request.item_id,
                        reason=request.reason,
                        command=request.command,
                        cwd=request.cwd,
                        details=request.details,
                        runtime_started_at_ms=request.runtime_started_at_ms,
                    )
                    session.add(approval)
                    session.flush()
                    append_task_event(
                        session,
                        task=task,
                        event_type="runtime.approval_requested",
                        aggregate_type="approval",
                        aggregate_id=approval.approval_id,
                        assignment_id=assignment.assignment_id,
                        runtime_execution_id=execution.runtime_execution_id,
                        source=f"runtime.{execution.provider}",
                        payload={
                            "approval_id": approval.approval_id,
                            "kind": approval.kind,
                            "status": ApprovalStatus.PENDING,
                        },
                    )
                    session.commit()
                if approval.status != ApprovalStatus.PENDING:
                    return self._decision_payload(approval)
                waiter = self._waiters.setdefault(approval.approval_id, Event())
                approval_id = approval.approval_id

        try:
            while True:
                with self.database.session() as session:
                    approval = session.get(ApprovalRequest, approval_id)
                    if approval is None:
                        raise LookupError(
                            f"approval '{approval_id}' does not exist"
                        )
                    if approval.status != ApprovalStatus.PENDING:
                        return self._decision_payload(approval)
                waiter.wait()
                waiter.clear()
        finally:
            with self._waiter_lock:
                self._waiters.pop(approval_id, None)

    def decide(
        self,
        *,
        approval_id: str,
        task_id: str,
        owner_user_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalRequest:
        """Persist one idempotent user decision and wake the Runtime worker."""

        with self._waiter_lock, self._mutation_lock, self.database.session() as session:
            approval = session.scalar(
                select(ApprovalRequest)
                .join(Task, Task.task_id == ApprovalRequest.task_id)
                .where(
                    ApprovalRequest.approval_id == approval_id,
                    ApprovalRequest.task_id == task_id,
                    Task.owner_user_id == owner_user_id,
                )
            )
            if approval is None:
                raise LookupError(f"approval '{approval_id}' does not exist")
            if approval.status != ApprovalStatus.PENDING:
                if approval.decision == decision:
                    return approval
                raise ValueError("approval has already been resolved")
            waiter = self._waiters.get(approval.approval_id)
            if waiter is None:
                raise RuntimeError("approval no longer has an active Runtime waiter")
            execution = session.get(RuntimeExecution, approval.runtime_execution_id)
            if (
                execution is None
                or execution.status != RuntimeExecutionStatus.WAITING
            ):
                raise RuntimeError("approval Runtime execution is no longer waiting")
            task = session.get(Task, approval.task_id)
            if task is None:
                raise LookupError(f"task '{approval.task_id}' does not exist")

            self._resolve(
                session=session,
                approval=approval,
                task=task,
                decision=decision,
                decided_by_user_id=owner_user_id,
                source="product",
                reason="user_decision",
            )
            session.commit()
            waiter.set()
            return approval

    def recover_orphaned_approvals(self) -> list[str]:
        """Cancel pending approvals whose Runtime execution is no longer waiting."""

        with self._waiter_lock, self._mutation_lock, self.database.session() as session:
            approvals = session.scalars(
                select(ApprovalRequest)
                .join(
                    RuntimeExecution,
                    RuntimeExecution.runtime_execution_id
                    == ApprovalRequest.runtime_execution_id,
                )
                .where(
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                    RuntimeExecution.status != RuntimeExecutionStatus.WAITING,
                )
            ).all()
            recovered: list[str] = []
            for approval in approvals:
                task = session.get(Task, approval.task_id)
                if task is None:
                    continue
                self._resolve(
                    session=session,
                    approval=approval,
                    task=task,
                    decision=ApprovalDecision.CANCEL,
                    decided_by_user_id=None,
                    source="runtime.supervisor",
                    reason="runtime_owner_lost",
                )
                recovered.append(approval.approval_id)
            session.commit()
            return recovered

    def cancel_task(
        self,
        *,
        task_id: str,
        reason: str = "task_cancelled",
    ) -> list[str]:
        """Resolve all pending approvals for a cancelled product Task."""

        with self._waiter_lock, self._mutation_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            approvals = session.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.task_id == task_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                )
            ).all()
            resolved: list[str] = []
            waiters: list[Event] = []
            for approval in approvals:
                self._resolve(
                    session=session,
                    approval=approval,
                    task=task,
                    decision=ApprovalDecision.CANCEL,
                    decided_by_user_id=None,
                    source="product",
                    reason=reason,
                )
                resolved.append(approval.approval_id)
                waiter = self._waiters.get(approval.approval_id)
                if waiter is not None:
                    waiters.append(waiter)
            session.commit()
            for waiter in waiters:
                waiter.set()
            return resolved

    def close(self) -> None:
        """Cancel active prompts so Runtime workers can stop cleanly."""

        with self._waiter_lock:
            if self._closed:
                return
            self._closed = True
            approval_ids = list(self._waiters)
            with self._mutation_lock, self.database.session() as session:
                for approval_id in approval_ids:
                    approval = session.get(ApprovalRequest, approval_id)
                    if approval is None or approval.status != ApprovalStatus.PENDING:
                        continue
                    task = session.get(Task, approval.task_id)
                    if task is None:
                        continue
                    self._resolve(
                        session=session,
                        approval=approval,
                        task=task,
                        decision=ApprovalDecision.CANCEL,
                        decided_by_user_id=None,
                        source="runtime.supervisor",
                        reason="runtime_shutdown",
                    )
                session.commit()
            for waiter in self._waiters.values():
                waiter.set()

    @staticmethod
    def _resolve(
        *,
        session,
        approval: ApprovalRequest,
        task: Task,
        decision: ApprovalDecision,
        decided_by_user_id: str | None,
        source: str,
        reason: str,
    ) -> None:
        status_by_decision = {
            ApprovalDecision.ACCEPT: ApprovalStatus.ACCEPTED,
            ApprovalDecision.DECLINE: ApprovalStatus.DECLINED,
            ApprovalDecision.CANCEL: ApprovalStatus.CANCELLED,
        }
        approval.status = status_by_decision[decision]
        approval.decision = decision
        approval.decided_by_user_id = decided_by_user_id
        approval.decided_at = utc_now()
        append_task_event(
            session,
            task=task,
            event_type="runtime.approval_resolved",
            aggregate_type="approval",
            aggregate_id=approval.approval_id,
            assignment_id=approval.assignment_id,
            runtime_execution_id=approval.runtime_execution_id,
            source=source,
            payload={
                "approval_id": approval.approval_id,
                "kind": approval.kind,
                "status": approval.status,
                "decision": approval.decision,
                "reason": reason,
            },
        )

    @staticmethod
    def _decision_payload(approval: ApprovalRequest) -> Mapping[str, str]:
        if approval.decision not in {
            ApprovalDecision.ACCEPT,
            ApprovalDecision.DECLINE,
            ApprovalDecision.CANCEL,
        }:
            raise RuntimeError("resolved approval has no supported decision")
        return {"decision": approval.decision}
