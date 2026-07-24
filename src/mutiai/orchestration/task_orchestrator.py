"""Product-owned task runner backed by a replaceable LangGraph graph."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from sqlalchemy import select

from mutiai.config import Settings
from mutiai.db import Database
from mutiai.domain import LeadReviewResult
from mutiai.models import Assignment, ProductEvent, RuntimeExecution, Task, Workspace
from mutiai.models.approval import ApprovalRequest, ApprovalStatus
from mutiai.models.base import utc_now
from mutiai.models.task import (
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskStatus,
)
from mutiai.orchestration.task_graph import (
    AssignmentResult,
    AssignmentWork,
    LeadReviewState,
    TaskGraphState,
    build_task_graph,
)
from mutiai.runtime import (
    AgentRuntimeAdapter,
    FakeRuntimeAdapter,
    RuntimeRecoveryRequest,
)
from mutiai.services.events import append_task_event
from mutiai.services.tasks import prepare_assignments, prepare_lead_review
from mutiai.services.workspaces import WorkspaceProvisioner


class TaskCancellationIncompleteError(RuntimeError):
    """The product cancelled a task but could not confirm every Runtime request."""

    def __init__(self, task_id: str, failures: dict[str, str]) -> None:
        self.task_id = task_id
        self.failures = dict(failures)
        super().__init__(
            f"task '{task_id}' cancellation was not confirmed by every Runtime"
        )


class TaskOrchestrator:
    """Runs the M1 graph while keeping durable facts in the product database."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime_adapter: AgentRuntimeAdapter | None = None,
        workspace_provisioner: WorkspaceProvisioner | None = None,
        mutation_lock: RLock | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
        self.workspace_provisioner = workspace_provisioner
        self._runtime_watch: Callable[[str], None] | None = None
        self._approval_canceller: Callable[..., list[str]] | None = None
        self._execution_lock = mutation_lock or RLock()
        self._graph_resume_lock = RLock()

    def set_runtime_watch(self, watch: Callable[[str], None]) -> None:
        """Register the post-checkpoint Runtime completion watcher."""

        self._runtime_watch = watch

    def set_approval_canceller(
        self,
        cancel: Callable[..., list[str]],
    ) -> None:
        """Register the product-owned pending approval cancellation boundary."""

        self._approval_canceller = cancel

    def run(self, task_id: str) -> Task:
        waiting_task: Task | None = None
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.NEEDS_REVISION,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return task
            if task.status == TaskStatus.WAITING:
                waiting_task = task
            else:
                assignments = prepare_assignments(
                    session,
                    task=task,
                    runtime_provider=self.runtime_adapter.provider,
                )
                initial_state: TaskGraphState = {
                    "task_id": task.task_id,
                    "assignments": [
                        self._work(assignment) for assignment in assignments
                    ],
                    "results": [],
                    "summary": "",
                    "review": None,
                }

        if waiting_task is not None:
            # Start workers only after the waiting state is durably checkpointed.
            self._watch_waiting_executions(waiting_task.task_id)
            return waiting_task

        checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        config = {"configurable": {"thread_id": task_id}}
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            graph = build_task_graph(
                self._execute_assignment,
                self._review_assignments,
            ).compile(checkpointer=saver)
            snapshot = graph.get_state(config)
            if snapshot.next:
                result = graph.invoke(None, config=config)
            elif snapshot.values and snapshot.values.get("review"):
                result = snapshot.values
            else:
                result = graph.invoke(initial_state, config=config)

        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status == TaskStatus.WAITING:
                waiting_task = task
            else:
                waiting_task = None

        if waiting_task is not None:
            # This is the first transition into waiting for a new graph run.
            self._watch_waiting_executions(task_id)
            return waiting_task

        review = result.get("review")
        if not review:
            raise RuntimeError(f"task '{task_id}' graph completed without a review")
        return self._finish_task(task_id, review)

    def retry(self, task_id: str) -> Task:
        """Reset only failed assignments, then resume the persisted graph."""

        with self._execution_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status != TaskStatus.FAILED:
                raise ValueError(f"task '{task_id}' is not failed")

            failed_assignments = session.scalars(
                select(Assignment).where(
                    Assignment.task_id == task_id,
                    Assignment.status == AssignmentStatus.FAILED,
                )
            ).all()
            if not failed_assignments:
                raise RuntimeError(
                    f"task '{task_id}' has no failed assignment to retry"
                )

            retry_payload: list[dict[str, str]] = []
            for assignment in failed_assignments:
                execution = assignment.runtime_execution
                if execution is None:
                    raise RuntimeError(
                        f"assignment '{assignment.assignment_id}' has no execution"
                    )
                previous_runtime_event_id = execution.runtime_event_id
                previous_turn_id = execution.turn_id
                execution.status = RuntimeExecutionStatus.SUBMITTED
                execution.runtime_job_id = None
                execution.runtime_event_id = None
                execution.turn_id = None
                execution.last_event_position = None
                execution.result_summary = None
                execution.started_at = None
                execution.completed_at = None
                assignment.status = AssignmentStatus.SUBMITTED
                assignment.result_summary = None
                assignment.completed_at = None
                retry_payload.append(
                    {
                        "assignment_id": assignment.assignment_id,
                        "execution_id": execution.execution_id,
                    }
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_retry_requested",
                    aggregate_type="runtime_execution",
                    aggregate_id=execution.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=execution.runtime_execution_id,
                    source="product",
                    payload={
                        "execution_id": execution.execution_id,
                        "previous_runtime_event_id": previous_runtime_event_id,
                        "previous_turn_id": previous_turn_id,
                        "status": RuntimeExecutionStatus.SUBMITTED,
                    },
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="assignment.status_changed",
                    aggregate_type="assignment",
                    aggregate_id=assignment.assignment_id,
                    assignment_id=assignment.assignment_id,
                    source="product",
                    payload={
                        "status": AssignmentStatus.SUBMITTED,
                        "reason": "retry",
                    },
                )

            task.status = TaskStatus.RUNNING
            task.result_summary = None
            task.completed_at = None
            task.updated_at = utc_now()
            append_task_event(
                session,
                task=task,
                event_type="task.retry_requested",
                aggregate_type="task",
                aggregate_id=task.task_id,
                source="product",
                payload={"assignments": retry_payload},
            )
            append_task_event(
                session,
                task=task,
                event_type="task.status_changed",
                aggregate_type="task",
                aggregate_id=task.task_id,
                source="langgraph",
                payload={"status": TaskStatus.RUNNING, "reason": "retry"},
            )
            session.commit()

        return self.run(task_id)

    def cancel(self, task_id: str) -> Task:
        """Cancel the product workflow and interrupt each live Runtime execution."""

        _, targets, already_cancelled = self._persist_task_cancellation(task_id)
        if already_cancelled:
            targets = self._unconfirmed_cancellation_targets(task_id)

        failures = self._dispatch_runtime_cancellations(task_id, targets)
        self._cancel_pending_approvals(task_id)
        if failures:
            raise TaskCancellationIncompleteError(task_id, failures)
        return self._load_task(task_id)

    def _unconfirmed_cancellation_targets(self, task_id: str) -> list[str]:
        terminal_event_types = {
            "runtime.execution_interrupt_requested",
            "runtime.execution_cancel_failed",
            "runtime.execution_cancelled",
        }
        with self.database.session() as session:
            executions = session.scalars(
                select(RuntimeExecution)
                .join(Assignment)
                .where(
                    Assignment.task_id == task_id,
                    RuntimeExecution.status == RuntimeExecutionStatus.CANCELLED,
                )
            ).all()
            targets: list[str] = []
            for execution in executions:
                latest = session.scalar(
                    select(ProductEvent)
                    .where(
                        ProductEvent.runtime_execution_id
                        == execution.runtime_execution_id,
                        ProductEvent.event_type.in_(terminal_event_types),
                    )
                    .order_by(ProductEvent.sequence.desc())
                    .limit(1)
                )
                if (
                    latest is not None
                    and latest.event_type == "runtime.execution_cancel_failed"
                ):
                    targets.append(execution.execution_id)
            return targets

    def cancel_runtime_execution(
        self,
        *,
        execution_id: str,
        runtime_event_id: str,
        terminal_status: str,
        runtime_job_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        reason: str = "runtime_cancelled",
    ) -> Task:
        """Persist a terminal Runtime interruption without resuming LangGraph."""

        with self.database.session() as session:
            execution = session.scalar(
                select(RuntimeExecution).where(
                    RuntimeExecution.execution_id == execution_id
                )
            )
            if execution is None:
                raise LookupError(f"execution '{execution_id}' does not exist")
            assignment = session.get(Assignment, execution.assignment_id)
            if assignment is None:
                raise LookupError(
                    f"assignment '{execution.assignment_id}' does not exist"
                )
            task_id = assignment.task_id

        task, targets, _ = self._persist_task_cancellation(
            task_id,
            terminal_execution_id=execution_id,
            runtime_event_id=runtime_event_id,
            terminal_status=terminal_status,
            runtime_job_id=runtime_job_id,
            thread_id=thread_id,
            turn_id=turn_id,
            reason=reason,
        )
        self._dispatch_runtime_cancellations(task_id, targets)
        self._cancel_pending_approvals(task_id)
        return task

    def _persist_task_cancellation(
        self,
        task_id: str,
        *,
        terminal_execution_id: str | None = None,
        runtime_event_id: str | None = None,
        terminal_status: str = "interrupted",
        runtime_job_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        reason: str = "task_cancelled",
    ) -> tuple[Task, list[str], bool]:
        targets: list[str] = []
        with self._execution_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.NEEDS_REVISION,
                TaskStatus.FAILED,
            }:
                if terminal_execution_id is None:
                    raise ValueError(
                        f"task '{task_id}' is already terminal in state "
                        f"'{task.status}'"
                    )
                return task, targets, False

            already_cancelled = task.status == TaskStatus.CANCELLED
            cancelled_at = utc_now()
            assignments = session.scalars(
                select(Assignment).where(Assignment.task_id == task_id)
            ).all()

            if not already_cancelled:
                append_task_event(
                    session,
                    task=task,
                    event_type="task.cancellation_requested",
                    aggregate_type="task",
                    aggregate_id=task.task_id,
                    source="product",
                    payload={"status": task.status},
                )

            for assignment in assignments:
                execution = assignment.runtime_execution
                if execution is not None:
                    previous_execution_status = execution.status
                    is_terminal_confirmation = (
                        execution.execution_id == terminal_execution_id
                    )
                    if execution.status != RuntimeExecutionStatus.COMPLETED:
                        execution.status = RuntimeExecutionStatus.CANCELLED
                        execution.completed_at = execution.completed_at or cancelled_at
                    if is_terminal_confirmation and (
                        execution.runtime_event_id is None
                        or execution.runtime_event_id == runtime_event_id
                    ):
                        is_new_terminal_event = execution.runtime_event_id is None
                        execution.runtime_event_id = runtime_event_id
                        execution.runtime_job_id = (
                            runtime_job_id or execution.runtime_job_id
                        )
                        execution.thread_id = thread_id or execution.thread_id
                        execution.turn_id = turn_id or execution.turn_id
                        if is_new_terminal_event:
                            append_task_event(
                                session,
                                task=task,
                                event_type="runtime.execution_cancelled",
                                aggregate_type="runtime_execution",
                                aggregate_id=execution.runtime_execution_id,
                                assignment_id=assignment.assignment_id,
                                runtime_execution_id=execution.runtime_execution_id,
                                source=f"runtime.{execution.provider}",
                                payload={
                                    "execution_id": execution.execution_id,
                                    "runtime_event_id": runtime_event_id,
                                    "runtime_job_id": execution.runtime_job_id,
                                    "thread_id": execution.thread_id,
                                    "turn_id": execution.turn_id,
                                    "terminal_status": terminal_status,
                                    "status": RuntimeExecutionStatus.CANCELLED,
                                    "reason": reason,
                                },
                            )
                    elif (
                        not already_cancelled
                        and previous_execution_status
                        in {
                            RuntimeExecutionStatus.RUNNING,
                            RuntimeExecutionStatus.WAITING,
                        }
                    ):
                        targets.append(execution.execution_id)
                        append_task_event(
                            session,
                            task=task,
                            event_type="runtime.execution_cancel_requested",
                            aggregate_type="runtime_execution",
                            aggregate_id=execution.runtime_execution_id,
                            assignment_id=assignment.assignment_id,
                            runtime_execution_id=execution.runtime_execution_id,
                            source="product",
                            payload={
                                "execution_id": execution.execution_id,
                                "runtime_job_id": execution.runtime_job_id,
                                "thread_id": execution.thread_id,
                                "turn_id": execution.turn_id,
                                "status": RuntimeExecutionStatus.CANCELLED,
                            },
                        )

                if assignment.status != AssignmentStatus.COMPLETED:
                    was_cancelled = assignment.status == AssignmentStatus.CANCELLED
                    assignment.status = AssignmentStatus.CANCELLED
                    assignment.completed_at = assignment.completed_at or cancelled_at
                    if not already_cancelled and not was_cancelled:
                        append_task_event(
                            session,
                            task=task,
                            event_type="assignment.status_changed",
                            aggregate_type="assignment",
                            aggregate_id=assignment.assignment_id,
                            assignment_id=assignment.assignment_id,
                            source="product",
                            payload={
                                "status": AssignmentStatus.CANCELLED,
                                "reason": reason,
                            },
                        )

            if not already_cancelled:
                task.status = TaskStatus.CANCELLED
                task.result_summary = None
                task.completed_at = cancelled_at
                task.updated_at = cancelled_at
                append_task_event(
                    session,
                    task=task,
                    event_type="task.status_changed",
                    aggregate_type="task",
                    aggregate_id=task.task_id,
                    source="product",
                    payload={
                        "status": TaskStatus.CANCELLED,
                        "reason": reason,
                    },
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="task.cancelled",
                    aggregate_type="task",
                    aggregate_id=task.task_id,
                    source="product",
                    payload={
                        "status": TaskStatus.CANCELLED,
                        "reason": reason,
                    },
                )
            session.commit()
            session.refresh(task)
            return task, targets, already_cancelled

    def _dispatch_runtime_cancellations(
        self,
        task_id: str,
        execution_ids: list[str],
    ) -> dict[str, str]:
        failures: dict[str, str] = {}
        for execution_id in execution_ids:
            try:
                accepted = self.runtime_adapter.cancel(execution_id)
                if not accepted:
                    raise RuntimeError("Runtime execution has no active owner")
            except Exception as exc:  # noqa: BLE001 - Runtime adapter boundary
                message = str(exc)[:1000]
                failures[execution_id] = message
                self._record_runtime_cancel_dispatch(
                    task_id=task_id,
                    execution_id=execution_id,
                    accepted=False,
                    error=message,
                )
            else:
                self._record_runtime_cancel_dispatch(
                    task_id=task_id,
                    execution_id=execution_id,
                    accepted=True,
                )
        return failures

    def _record_runtime_cancel_dispatch(
        self,
        *,
        task_id: str,
        execution_id: str,
        accepted: bool,
        error: str | None = None,
    ) -> None:
        with self._execution_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            execution = session.scalar(
                select(RuntimeExecution).where(
                    RuntimeExecution.execution_id == execution_id
                )
            )
            if task is None or execution is None:
                raise LookupError("task or Runtime execution disappeared")
            assignment = session.get(Assignment, execution.assignment_id)
            if assignment is None:
                raise LookupError(
                    f"assignment '{execution.assignment_id}' does not exist"
                )
            append_task_event(
                session,
                task=task,
                event_type=(
                    "runtime.execution_interrupt_requested"
                    if accepted
                    else "runtime.execution_cancel_failed"
                ),
                aggregate_type="runtime_execution",
                aggregate_id=execution.runtime_execution_id,
                assignment_id=assignment.assignment_id,
                runtime_execution_id=execution.runtime_execution_id,
                source=f"runtime.{execution.provider}",
                payload={
                    "execution_id": execution.execution_id,
                    "runtime_job_id": execution.runtime_job_id,
                    "thread_id": execution.thread_id,
                    "turn_id": execution.turn_id,
                    "status": RuntimeExecutionStatus.CANCELLED,
                    "accepted": accepted,
                    **(
                        {
                            "reason": "runtime_cancel_unconfirmed",
                            "error": error,
                        }
                        if error is not None
                        else {}
                    ),
                },
            )
            session.commit()

    def _cancel_pending_approvals(self, task_id: str) -> None:
        if self._approval_canceller is not None:
            self._approval_canceller(task_id=task_id, reason="task_cancelled")

    def _load_task(self, task_id: str) -> Task:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            return task

    def _watch_waiting_executions(self, task_id: str) -> None:
        if self._runtime_watch is None:
            return
        with self.database.session() as session:
            execution_ids = session.scalars(
                select(RuntimeExecution.execution_id)
                .join(Assignment)
                .where(
                    Assignment.task_id == task_id,
                    RuntimeExecution.status == RuntimeExecutionStatus.WAITING,
                )
            ).all()
        for execution_id in execution_ids:
            self._runtime_watch(execution_id)

    def complete_runtime_execution(
        self,
        *,
        execution_id: str,
        runtime_event_id: str,
        summary: str,
        runtime_job_id: str | None = None,
        last_event_position: str | None = None,
    ) -> Task:
        """Persist one external completion event and resume its waiting graph node."""

        should_resume = True
        with self._execution_lock, self.database.session() as session:
            execution = session.scalar(
                select(RuntimeExecution).where(
                    RuntimeExecution.execution_id == execution_id
                )
            )
            if execution is None:
                raise LookupError(f"execution '{execution_id}' does not exist")
            assignment = session.get(Assignment, execution.assignment_id)
            if assignment is None:
                raise LookupError(
                    f"assignment '{execution.assignment_id}' does not exist"
                )
            task = session.get(Task, assignment.task_id)
            if task is None:
                raise LookupError(f"task '{assignment.task_id}' does not exist")

            if (
                execution.status == RuntimeExecutionStatus.CANCELLED
                or task.status == TaskStatus.CANCELLED
            ):
                return task

            if execution.status == RuntimeExecutionStatus.COMPLETED:
                if execution.runtime_event_id != runtime_event_id:
                    raise ValueError(
                        "execution already completed with another Runtime event"
                    )
                task_id = task.task_id
            else:
                if execution.status != RuntimeExecutionStatus.WAITING:
                    raise ValueError(
                        f"cannot complete execution in state '{execution.status}'"
                    )
                completed_at = utc_now()
                execution.status = RuntimeExecutionStatus.COMPLETED
                execution.runtime_event_id = runtime_event_id
                execution.runtime_job_id = (
                    runtime_job_id or execution.runtime_job_id
                )
                execution.last_event_position = last_event_position
                execution.result_summary = summary
                execution.completed_at = completed_at
                assignment.status = AssignmentStatus.COMPLETED
                assignment.result_summary = summary
                assignment.completed_at = completed_at
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_completed",
                    aggregate_type="runtime_execution",
                    aggregate_id=execution.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=execution.runtime_execution_id,
                    source=f"runtime.{execution.provider}",
                    payload={
                        "execution_id": execution.execution_id,
                        "runtime_event_id": runtime_event_id,
                        "runtime_job_id": execution.runtime_job_id,
                        "status": RuntimeExecutionStatus.COMPLETED,
                        "summary": summary,
                    },
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="assignment.status_changed",
                    aggregate_type="assignment",
                    aggregate_id=assignment.assignment_id,
                    assignment_id=assignment.assignment_id,
                    source="langgraph",
                    payload={
                        "status": AssignmentStatus.COMPLETED,
                        "summary": summary,
                    },
                )
            session.commit()
            task_id = task.task_id
            should_resume = task.status not in {
                TaskStatus.FAILED,
                TaskStatus.COMPLETED,
                TaskStatus.NEEDS_REVISION,
                TaskStatus.CANCELLED,
            }

        if not should_resume:
            return task

        return self._resume_runtime_interrupt(
            task_id=task_id,
            execution_id=execution_id,
            runtime_event_id=runtime_event_id,
        )

    def fail_runtime_execution(
        self,
        *,
        execution_id: str,
        runtime_event_id: str,
        terminal_status: str,
        error: str,
        runtime_job_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        reason: str = "runtime_terminal_failure",
        source: str | None = None,
    ) -> Task:
        """Persist one terminal Runtime failure without resuming the graph."""

        with self._execution_lock, self.database.session() as session:
            execution = session.scalar(
                select(RuntimeExecution).where(
                    RuntimeExecution.execution_id == execution_id
                )
            )
            if execution is None:
                raise LookupError(f"execution '{execution_id}' does not exist")
            assignment = session.get(Assignment, execution.assignment_id)
            if assignment is None:
                raise LookupError(
                    f"assignment '{execution.assignment_id}' does not exist"
                )
            task = session.get(Task, assignment.task_id)
            if task is None:
                raise LookupError(f"task '{assignment.task_id}' does not exist")

            if (
                execution.status == RuntimeExecutionStatus.CANCELLED
                or task.status == TaskStatus.CANCELLED
            ):
                return task

            if execution.status == RuntimeExecutionStatus.FAILED:
                if execution.runtime_event_id != runtime_event_id:
                    raise ValueError(
                        "execution already failed with another Runtime event"
                    )
                return task
            if execution.status != RuntimeExecutionStatus.WAITING:
                raise ValueError(
                    f"cannot fail execution in state '{execution.status}'"
                )

            failed_at = utc_now()
            execution.status = RuntimeExecutionStatus.FAILED
            execution.runtime_event_id = runtime_event_id
            execution.runtime_job_id = runtime_job_id or execution.runtime_job_id
            execution.thread_id = thread_id or execution.thread_id
            execution.turn_id = turn_id or execution.turn_id
            execution.completed_at = failed_at
            assignment.status = AssignmentStatus.FAILED
            assignment.completed_at = failed_at
            task.status = TaskStatus.FAILED
            task.result_summary = None
            task.completed_at = None
            task.updated_at = failed_at
            append_task_event(
                session,
                task=task,
                event_type="runtime.execution_failed",
                aggregate_type="runtime_execution",
                aggregate_id=execution.runtime_execution_id,
                assignment_id=assignment.assignment_id,
                runtime_execution_id=execution.runtime_execution_id,
                source=source or f"runtime.{execution.provider}",
                payload={
                    "execution_id": execution.execution_id,
                    "runtime_event_id": runtime_event_id,
                    "runtime_job_id": execution.runtime_job_id,
                    "thread_id": execution.thread_id,
                    "turn_id": execution.turn_id,
                    "terminal_status": terminal_status,
                    "status": RuntimeExecutionStatus.FAILED,
                    "reason": reason,
                    "error": error[:1000],
                },
            )
            append_task_event(
                session,
                task=task,
                event_type="assignment.status_changed",
                aggregate_type="assignment",
                aggregate_id=assignment.assignment_id,
                assignment_id=assignment.assignment_id,
                source="langgraph",
                payload={
                    "status": AssignmentStatus.FAILED,
                    "reason": reason,
                },
            )
            append_task_event(
                session,
                task=task,
                event_type="task.failed",
                aggregate_type="task",
                aggregate_id=task.task_id,
                source="langgraph",
                payload={
                    "status": TaskStatus.FAILED,
                    "reason": reason,
                    "execution_id": execution.execution_id,
                },
            )
            session.commit()
            session.refresh(task)
            return task

    def recover_orphaned_runtime_executions(
        self,
        *,
        is_active: Callable[[str], bool],
        try_recover: Callable[[RuntimeRecoveryRequest], bool] | None = None,
    ) -> list[str]:
        """Reattach waiting executions or fail those with no recoverable owner.

        An external App Server endpoint can keep a Turn alive across backend
        restarts. Without that endpoint, or when identity validation fails, the
        execution becomes an explicit, user-retryable failure instead of being
        replayed implicitly.
        """

        with self.database.session() as session:
            waiting_executions = session.execute(
                select(
                    RuntimeExecution.execution_id,
                    RuntimeExecution.runtime_job_id,
                    RuntimeExecution.thread_id,
                    RuntimeExecution.turn_id,
                    RuntimeExecution.workspace_id,
                    Workspace.canonical_path,
                    select(ApprovalRequest.approval_id)
                    .where(
                        ApprovalRequest.runtime_execution_id
                        == RuntimeExecution.runtime_execution_id,
                        ApprovalRequest.status == ApprovalStatus.PENDING,
                    )
                    .exists()
                    .label("has_pending_approval"),
                )
                .outerjoin(
                    Workspace,
                    Workspace.workspace_id == RuntimeExecution.workspace_id,
                ).where(
                    RuntimeExecution.provider == self.runtime_adapter.provider,
                    RuntimeExecution.status == RuntimeExecutionStatus.WAITING,
                )
            ).all()

        recovered: list[str] = []
        for (
            execution_id,
            runtime_job_id,
            thread_id,
            turn_id,
            workspace_id,
            workspace_path,
            has_pending_approval,
        ) in waiting_executions:
            if is_active(execution_id):
                continue
            recovery_error: str | None = None
            if has_pending_approval:
                recovery_error = (
                    "Transparent recovery is disabled while a Runtime approval "
                    "request is pending; explicit retry is required."
                )
            elif (
                try_recover is not None
                and thread_id is not None
                and turn_id is not None
                and workspace_id is not None
                and workspace_path is not None
            ):
                try:
                    reattached = try_recover(
                        RuntimeRecoveryRequest(
                            execution_id=execution_id,
                            runtime_job_id=runtime_job_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            workspace_id=workspace_id,
                            workspace_path=workspace_path,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - recovery boundary
                    recovery_error = str(exc)[:500]
                else:
                    if reattached:
                        self._record_runtime_reconnected(execution_id)
                        if self._runtime_watch is None:
                            raise RuntimeError(
                                "reattached Runtime execution has no supervisor"
                            )
                        self._runtime_watch(execution_id)
                        continue
            recovery_key = hashlib.sha256(
                f"{execution_id}:{turn_id or 'unknown'}".encode()
            ).hexdigest()
            error = (
                "The Runtime owner process ended while this Turn was waiting; "
                "an explicit retry is required."
            )
            if recovery_error:
                error = f"{error} Reattach failed: {recovery_error}"
            self.fail_runtime_execution(
                execution_id=execution_id,
                runtime_event_id=f"runtime-recovery:{recovery_key}",
                terminal_status="orphaned",
                error=error,
                reason="runtime_owner_lost",
                source="runtime.supervisor",
            )
            recovered.append(execution_id)
        return recovered

    def _record_runtime_reconnected(self, execution_id: str) -> None:
        with self._execution_lock, self.database.session() as session:
            execution = session.scalar(
                select(RuntimeExecution).where(
                    RuntimeExecution.execution_id == execution_id
                )
            )
            if execution is None:
                raise LookupError(f"execution '{execution_id}' does not exist")
            assignment = session.get(Assignment, execution.assignment_id)
            if assignment is None:
                raise LookupError(
                    f"assignment '{execution.assignment_id}' does not exist"
                )
            task = session.get(Task, assignment.task_id)
            if task is None:
                raise LookupError(f"task '{assignment.task_id}' does not exist")
            prior_reconnects = session.scalars(
                select(ProductEvent).where(
                    ProductEvent.event_type == "runtime.execution_reconnected",
                    ProductEvent.runtime_execution_id
                    == execution.runtime_execution_id,
                )
            ).all()
            if any(
                event.payload.get("turn_id") == execution.turn_id
                for event in prior_reconnects
            ):
                return
            append_task_event(
                session,
                task=task,
                event_type="runtime.execution_reconnected",
                aggregate_type="runtime_execution",
                aggregate_id=execution.runtime_execution_id,
                assignment_id=assignment.assignment_id,
                runtime_execution_id=execution.runtime_execution_id,
                source="runtime.supervisor",
                payload={
                    "execution_id": execution.execution_id,
                    "runtime_job_id": execution.runtime_job_id,
                    "thread_id": execution.thread_id,
                    "turn_id": execution.turn_id,
                    "status": execution.status,
                },
            )
            session.commit()

    def record_runtime_watch_error(
        self,
        *,
        execution_id: str,
        error: str,
    ) -> None:
        """Persist a supervisor error without copying Runtime internals to State."""

        with self._execution_lock, self.database.session() as session:
            execution = session.scalar(
                select(RuntimeExecution).where(
                    RuntimeExecution.execution_id == execution_id
                )
            )
            if execution is None:
                raise LookupError(f"execution '{execution_id}' does not exist")
            assignment = session.get(Assignment, execution.assignment_id)
            if assignment is None:
                raise LookupError(
                    f"assignment '{execution.assignment_id}' does not exist"
                )
            task = session.get(Task, assignment.task_id)
            if task is None:
                raise LookupError(f"task '{assignment.task_id}' does not exist")
            append_task_event(
                session,
                task=task,
                event_type="runtime.execution_watch_failed",
                aggregate_type="runtime_execution",
                aggregate_id=execution.runtime_execution_id,
                assignment_id=assignment.assignment_id,
                runtime_execution_id=execution.runtime_execution_id,
                source="runtime.supervisor",
                payload={
                    "execution_id": execution.execution_id,
                    "status": execution.status,
                    "error": error[:1000],
                },
            )
            session.commit()

    @staticmethod
    def _work(
        assignment: Assignment,
        *,
        output_schema: dict[str, Any] | None = None,
    ) -> AssignmentWork:
        return {
            "assignment_id": assignment.assignment_id,
            "execution_id": assignment.execution_id,
            "role_key": assignment.agent_role_key,
            "instructions": assignment.instructions,
            "output_schema": output_schema,
        }

    def _review_assignments(
        self,
        task_id: str,
        results: list[AssignmentResult],
    ) -> LeadReviewState:
        with self._execution_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status == TaskStatus.CANCELLED:
                return self._cancelled_review()
            assignment = prepare_lead_review(
                session,
                task=task,
                specialist_results=results,
                runtime_provider=self.runtime_adapter.provider,
            )
            work = self._work(
                assignment,
                output_schema=LeadReviewResult.model_json_schema(),
            )

        result = self._execute_assignment(work)
        try:
            review = LeadReviewResult.model_validate_json(result["summary"])
        except (ValidationError, ValueError) as exc:
            if self._task_is_cancelled(task_id):
                return self._cancelled_review()
            self._record_invalid_lead_review(
                task_id=task_id,
                assignment_id=result["assignment_id"],
                error=str(exc),
            )
            raise RuntimeError("organization lead returned an invalid review") from exc

        with self._execution_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            assignment = session.get(Assignment, result["assignment_id"])
            if task is None or assignment is None:
                raise LookupError("lead review records are unavailable")
            if task.status == TaskStatus.CANCELLED:
                return self._cancelled_review()
            existing_event = session.scalar(
                select(ProductEvent).where(
                    ProductEvent.task_id == task_id,
                    ProductEvent.assignment_id == assignment.assignment_id,
                    ProductEvent.event_type == "lead.review_completed",
                )
            )
            if existing_event is None:
                append_task_event(
                    session,
                    task=task,
                    event_type="lead.review_completed",
                    aggregate_type="assignment",
                    aggregate_id=assignment.assignment_id,
                    assignment_id=assignment.assignment_id,
                    source=f"runtime.{self.runtime_adapter.provider}",
                    payload=review.model_dump(mode="json"),
                )
                session.commit()

        return {
            "decision": review.decision,
            "final_summary": review.final_summary,
            "issues": list(review.issues),
        }

    def _task_is_cancelled(self, task_id: str) -> bool:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            return task.status == TaskStatus.CANCELLED

    @staticmethod
    def _cancelled_review() -> LeadReviewState:
        return {
            "decision": "needs_revision",
            "final_summary": "Task cancelled before organization-lead review.",
            "issues": [],
        }

    def _record_invalid_lead_review(
        self,
        *,
        task_id: str,
        assignment_id: str,
        error: str,
    ) -> None:
        with self._execution_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            assignment = session.get(Assignment, assignment_id)
            if task is None or assignment is None:
                raise LookupError("lead review records are unavailable")
            if task.status == TaskStatus.CANCELLED:
                return
            execution = assignment.runtime_execution
            if execution is None:
                raise LookupError("lead review execution is unavailable")
            assignment.status = AssignmentStatus.FAILED
            assignment.completed_at = utc_now()
            task.status = TaskStatus.FAILED
            task.updated_at = utc_now()
            append_task_event(
                session,
                task=task,
                event_type="lead.review_invalid",
                aggregate_type="assignment",
                aggregate_id=assignment.assignment_id,
                assignment_id=assignment.assignment_id,
                source="langgraph",
                payload={"error": error[:1000]},
            )
            append_task_event(
                session,
                task=task,
                event_type="assignment.status_changed",
                aggregate_type="assignment",
                aggregate_id=assignment.assignment_id,
                assignment_id=assignment.assignment_id,
                source="langgraph",
                payload={
                    "status": AssignmentStatus.FAILED,
                    "reason": "invalid_lead_review",
                },
            )
            append_task_event(
                session,
                task=task,
                event_type="task.failed",
                aggregate_type="task",
                aggregate_id=task.task_id,
                source="langgraph",
                payload={
                    "status": TaskStatus.FAILED,
                    "reason": "invalid_lead_review",
                },
            )
            session.commit()

    def _execute_assignment(self, work: AssignmentWork) -> AssignmentResult:
        with self._execution_lock, self.database.session() as session:
            execution = session.scalar(
                select(RuntimeExecution).where(
                    RuntimeExecution.execution_id == work["execution_id"]
                )
            )
            assignment = session.get(Assignment, work["assignment_id"])
            if execution is None or assignment is None:
                raise LookupError(
                    f"execution '{work['execution_id']}' is not prepared"
                )
            task = session.get(Task, assignment.task_id)
            if task is None:
                raise LookupError(f"task '{assignment.task_id}' does not exist")
            if task.status == TaskStatus.CANCELLED:
                return {
                    "assignment_id": assignment.assignment_id,
                    "execution_id": assignment.execution_id,
                    "role_key": assignment.agent_role_key,
                    "summary": execution.result_summary or "",
                }
            if execution.status == RuntimeExecutionStatus.COMPLETED:
                return {
                    "assignment_id": assignment.assignment_id,
                    "execution_id": assignment.execution_id,
                    "role_key": assignment.agent_role_key,
                    "summary": execution.result_summary or "",
                }
            if execution.status == RuntimeExecutionStatus.FAILED:
                self._interrupt_for_runtime_retry(task, assignment, execution)
            if execution.status == RuntimeExecutionStatus.WAITING:
                self._interrupt_for_runtime(task, assignment, execution)

            workspace = None
            if (
                self.runtime_adapter.provider == "codex"
                and self.workspace_provisioner is not None
            ):
                workspace = self.workspace_provisioner.ensure_role_workspace(
                    session,
                    owner_user_id=task.owner_user_id,
                    organization_id=task.organization_id,
                    agent_role_key=assignment.agent_role_key,
                    runtime_provider=self.runtime_adapter.provider,
                )
                execution.workspace_id = workspace.workspace_id

            now = utc_now()
            execution.status = RuntimeExecutionStatus.RUNNING
            execution.started_at = execution.started_at or now
            assignment.status = AssignmentStatus.RUNNING
            append_task_event(
                session,
                task=task,
                event_type="runtime.execution_started",
                aggregate_type="runtime_execution",
                aggregate_id=execution.runtime_execution_id,
                assignment_id=assignment.assignment_id,
                runtime_execution_id=execution.runtime_execution_id,
                source=f"runtime.{self.runtime_adapter.provider}",
                payload={
                    "execution_id": execution.execution_id,
                    "status": RuntimeExecutionStatus.RUNNING,
                },
            )
            append_task_event(
                session,
                task=task,
                event_type="assignment.status_changed",
                aggregate_type="assignment",
                aggregate_id=assignment.assignment_id,
                assignment_id=assignment.assignment_id,
                source="langgraph",
                payload={"status": AssignmentStatus.RUNNING},
            )
            session.commit()

            try:
                runtime_result = self.runtime_adapter.execute(
                    execution_id=work["execution_id"],
                    role_key=work["role_key"],
                    instructions=work["instructions"],
                    workspace_id=workspace.workspace_id if workspace else None,
                    workspace_path=workspace.canonical_path if workspace else None,
                    thread_id=workspace.codex_thread_id if workspace else None,
                    output_schema=work["output_schema"],
                )
            except Exception:
                failed_at = utc_now()
                execution.status = RuntimeExecutionStatus.FAILED
                assignment.status = AssignmentStatus.FAILED
                task.status = TaskStatus.FAILED
                task.updated_at = failed_at
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_failed",
                    aggregate_type="runtime_execution",
                    aggregate_id=execution.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=execution.runtime_execution_id,
                    source=f"runtime.{self.runtime_adapter.provider}",
                    payload={
                        "execution_id": execution.execution_id,
                        "status": RuntimeExecutionStatus.FAILED,
                    },
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="assignment.status_changed",
                    aggregate_type="assignment",
                    aggregate_id=assignment.assignment_id,
                    assignment_id=assignment.assignment_id,
                    source="langgraph",
                    payload={"status": AssignmentStatus.FAILED},
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="task.failed",
                    aggregate_type="task",
                    aggregate_id=task.task_id,
                    source="langgraph",
                    payload={"status": TaskStatus.FAILED},
                )
                session.commit()
                raise

            if runtime_result.status == "waiting":
                execution.status = RuntimeExecutionStatus.WAITING
                execution.runtime_job_id = runtime_result.runtime_job_id
                execution.thread_id = runtime_result.thread_id
                execution.turn_id = runtime_result.turn_id
                execution.workspace_id = runtime_result.workspace_id
                execution.last_event_position = runtime_result.last_event_position
                if workspace is not None and runtime_result.thread_id is not None:
                    if (
                        workspace.codex_thread_id is not None
                        and workspace.codex_thread_id != runtime_result.thread_id
                    ):
                        raise RuntimeError(
                            "Codex Runtime returned a different Thread for the "
                            "existing Workspace"
                        )
                    workspace.codex_thread_id = runtime_result.thread_id
                assignment.status = AssignmentStatus.WAITING
                if task.status != TaskStatus.WAITING:
                    task.status = TaskStatus.WAITING
                    task.updated_at = utc_now()
                    append_task_event(
                        session,
                        task=task,
                        event_type="task.status_changed",
                        aggregate_type="task",
                        aggregate_id=task.task_id,
                        source="langgraph",
                        payload={"status": TaskStatus.WAITING},
                    )
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_waiting",
                    aggregate_type="runtime_execution",
                    aggregate_id=execution.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=execution.runtime_execution_id,
                    source=f"runtime.{self.runtime_adapter.provider}",
                    payload={
                        "execution_id": execution.execution_id,
                        "runtime_job_id": runtime_result.runtime_job_id,
                        "status": RuntimeExecutionStatus.WAITING,
                    },
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="assignment.status_changed",
                    aggregate_type="assignment",
                    aggregate_id=assignment.assignment_id,
                    assignment_id=assignment.assignment_id,
                    source="langgraph",
                    payload={"status": AssignmentStatus.WAITING},
                )
                session.commit()
                self._interrupt_for_runtime(task, assignment, execution)

            if runtime_result.summary is None:
                raise RuntimeError(
                    f"completed execution '{execution.execution_id}' has no summary"
                )

            completed_at = utc_now()
            execution.status = RuntimeExecutionStatus.COMPLETED
            execution.runtime_job_id = runtime_result.runtime_job_id
            execution.thread_id = runtime_result.thread_id
            execution.turn_id = runtime_result.turn_id
            execution.workspace_id = runtime_result.workspace_id
            execution.last_event_position = runtime_result.last_event_position
            execution.result_summary = runtime_result.summary
            execution.completed_at = completed_at
            assignment.status = AssignmentStatus.COMPLETED
            assignment.result_summary = runtime_result.summary
            assignment.completed_at = completed_at
            append_task_event(
                session,
                task=task,
                event_type="runtime.execution_completed",
                aggregate_type="runtime_execution",
                aggregate_id=execution.runtime_execution_id,
                assignment_id=assignment.assignment_id,
                runtime_execution_id=execution.runtime_execution_id,
                source=f"runtime.{self.runtime_adapter.provider}",
                payload={
                    "execution_id": execution.execution_id,
                    "runtime_job_id": runtime_result.runtime_job_id,
                    "status": RuntimeExecutionStatus.COMPLETED,
                    "summary": runtime_result.summary,
                },
            )
            append_task_event(
                session,
                task=task,
                event_type="assignment.status_changed",
                aggregate_type="assignment",
                aggregate_id=assignment.assignment_id,
                assignment_id=assignment.assignment_id,
                source="langgraph",
                payload={
                    "status": AssignmentStatus.COMPLETED,
                    "summary": runtime_result.summary,
                },
            )
            session.commit()
            return {
                "assignment_id": assignment.assignment_id,
                "execution_id": assignment.execution_id,
                "role_key": assignment.agent_role_key,
                "summary": runtime_result.summary,
            }

    @staticmethod
    def _interrupt_for_runtime(
        task: Task,
        assignment: Assignment,
        execution: RuntimeExecution,
    ) -> None:
        interrupt(
            {
                "kind": "runtime.waiting",
                "task_id": task.task_id,
                "assignment_id": assignment.assignment_id,
                "execution_id": execution.execution_id,
                "runtime_execution_id": execution.runtime_execution_id,
                "runtime_job_id": execution.runtime_job_id,
            }
        )
        raise RuntimeError(
            f"execution '{execution.execution_id}' resumed before completion"
        )

    @staticmethod
    def _interrupt_for_runtime_retry(
        task: Task,
        assignment: Assignment,
        execution: RuntimeExecution,
    ) -> None:
        interrupt(
            {
                "kind": "runtime.retry_required",
                "task_id": task.task_id,
                "assignment_id": assignment.assignment_id,
                "execution_id": execution.execution_id,
                "runtime_execution_id": execution.runtime_execution_id,
            }
        )
        raise RuntimeError(
            f"execution '{execution.execution_id}' resumed before retry"
        )

    def _resume_runtime_interrupt(
        self,
        *,
        task_id: str,
        execution_id: str,
        runtime_event_id: str,
    ) -> Task:
        with self._graph_resume_lock:
            with self.database.session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    raise LookupError(f"task '{task_id}' does not exist")
                if task.status in {
                    TaskStatus.FAILED,
                    TaskStatus.COMPLETED,
                    TaskStatus.NEEDS_REVISION,
                    TaskStatus.CANCELLED,
                }:
                    return task

            checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
            config = {"configurable": {"thread_id": task_id}}
            with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                graph = build_task_graph(
                    self._execute_assignment,
                    self._review_assignments,
                ).compile(checkpointer=saver)
                snapshot = graph.get_state(config)
                matching_interrupts = [
                    item
                    for item in snapshot.interrupts
                    if isinstance(item.value, dict)
                    and item.value.get("execution_id") == execution_id
                ]
                if matching_interrupts:
                    resume_payload = {
                        item.id: {
                            "runtime_event_id": runtime_event_id,
                            "execution_id": execution_id,
                        }
                        for item in matching_interrupts
                    }
                    result = graph.invoke(
                        Command(resume=resume_payload),
                        config=config,
                    )
                elif snapshot.values and snapshot.values.get("review"):
                    result = snapshot.values
                elif snapshot.interrupts:
                    result = None
                else:
                    raise RuntimeError(
                        f"task '{task_id}' has no matching Runtime interrupt"
                    )

                remaining_interrupts = graph.get_state(config).interrupts

            with self.database.session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    raise LookupError(f"task '{task_id}' does not exist")
                if remaining_interrupts:
                    self._watch_waiting_executions(task_id)
                    return task

            if result is None:
                raise RuntimeError(f"task '{task_id}' resumed without a result")
            review = result.get("review")
            if not review:
                raise RuntimeError(f"task '{task_id}' resumed without a review")
            return self._finish_task(task_id, review)

    def _finish_task(self, task_id: str, review_payload: dict) -> Task:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status == TaskStatus.CANCELLED:
                return task
            review = LeadReviewResult.model_validate(review_payload)
            terminal_status = (
                TaskStatus.COMPLETED
                if review.decision == "accepted"
                else TaskStatus.NEEDS_REVISION
            )
            if task.status != terminal_status:
                now = utc_now()
                task.status = terminal_status
                task.result_summary = review.final_summary
                task.completed_at = (
                    now if terminal_status == TaskStatus.COMPLETED else None
                )
                task.updated_at = now
                append_task_event(
                    session,
                    task=task,
                    event_type=(
                        "task.completed"
                        if terminal_status == TaskStatus.COMPLETED
                        else "task.needs_revision"
                    ),
                    aggregate_type="task",
                    aggregate_id=task.task_id,
                    source="langgraph",
                    payload={
                        "status": terminal_status,
                        "decision": review.decision,
                        "summary": review.final_summary,
                        "issues": list(review.issues),
                    },
                )
                session.commit()
                session.refresh(task)
            return task
