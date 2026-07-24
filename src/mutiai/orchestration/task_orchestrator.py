"""Product-owned task runner backed by a replaceable LangGraph graph."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from sqlalchemy import select

from mutiai.config import Settings
from mutiai.db import Database
from mutiai.models import Assignment, RuntimeExecution, Task
from mutiai.models.base import utc_now
from mutiai.models.task import (
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskStatus,
)
from mutiai.orchestration.task_graph import (
    AssignmentResult,
    AssignmentWork,
    TaskGraphState,
    build_task_graph,
)
from mutiai.runtime import AgentRuntimeAdapter, FakeRuntimeAdapter
from mutiai.services.events import append_task_event
from mutiai.services.tasks import prepare_assignments
from mutiai.services.workspaces import WorkspaceProvisioner


class TaskOrchestrator:
    """Runs the M1 graph while keeping durable facts in the product database."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime_adapter: AgentRuntimeAdapter | None = None,
        workspace_provisioner: WorkspaceProvisioner | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
        self.workspace_provisioner = workspace_provisioner
        self._runtime_watch: Callable[[str], None] | None = None
        self._execution_lock = RLock()
        self._graph_resume_lock = RLock()

    def set_runtime_watch(self, watch: Callable[[str], None]) -> None:
        """Register the post-checkpoint Runtime completion watcher."""

        self._runtime_watch = watch

    def run(self, task_id: str) -> Task:
        waiting_task: Task | None = None
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status == TaskStatus.COMPLETED:
                return task
            if task.status == TaskStatus.WAITING:
                waiting_task = task
            else:
                assignments = prepare_assignments(
                    session,
                    task=task,
                    runtime_provider=self.runtime_adapter.provider,
                )
                if task.status == TaskStatus.FAILED:
                    task.status = TaskStatus.RUNNING
                    task.updated_at = utc_now()
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
                initial_state: TaskGraphState = {
                    "task_id": task.task_id,
                    "assignments": [
                        self._work(assignment) for assignment in assignments
                    ],
                    "results": [],
                    "summary": "",
                }

        if waiting_task is not None:
            # Start workers only after the waiting state is durably checkpointed.
            self._watch_waiting_executions(waiting_task.task_id)
            return waiting_task

        checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        config = {"configurable": {"thread_id": task_id}}
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            graph = build_task_graph(self._execute_assignment).compile(
                checkpointer=saver
            )
            snapshot = graph.get_state(config)
            if snapshot.next:
                result = graph.invoke(None, config=config)
            elif snapshot.values and snapshot.values.get("summary"):
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

        summary = result.get("summary")
        if not summary:
            raise RuntimeError(f"task '{task_id}' graph completed without a summary")
        return self._complete_task(task_id, summary)

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

        with self._execution_lock:
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
                task = session.get(Task, assignment.task_id)
                if task is None:
                    raise LookupError(f"task '{assignment.task_id}' does not exist")

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

        return self._resume_runtime_interrupt(
            task_id=task_id,
            execution_id=execution_id,
            runtime_event_id=runtime_event_id,
        )

    def record_runtime_watch_error(
        self,
        *,
        execution_id: str,
        error: str,
    ) -> None:
        """Persist a supervisor error without copying Runtime internals to State."""

        with self._execution_lock:
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
    def _work(assignment: Assignment) -> AssignmentWork:
        return {
            "assignment_id": assignment.assignment_id,
            "execution_id": assignment.execution_id,
            "role_key": assignment.agent_role_key,
            "instructions": assignment.instructions,
        }

    def _execute_assignment(self, work: AssignmentWork) -> AssignmentResult:
        with self._execution_lock:
            with self.database.session() as session:
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
                if execution.status == RuntimeExecutionStatus.COMPLETED:
                    return {
                        "assignment_id": assignment.assignment_id,
                        "execution_id": assignment.execution_id,
                        "role_key": assignment.agent_role_key,
                        "summary": execution.result_summary or "",
                    }
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

    def _resume_runtime_interrupt(
        self,
        *,
        task_id: str,
        execution_id: str,
        runtime_event_id: str,
    ) -> Task:
        with self._graph_resume_lock:
            checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
            config = {"configurable": {"thread_id": task_id}}
            with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                graph = build_task_graph(self._execute_assignment).compile(
                    checkpointer=saver
                )
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
                elif snapshot.values and snapshot.values.get("summary"):
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
                    return task

            if result is None:
                raise RuntimeError(f"task '{task_id}' resumed without a result")
            summary = result.get("summary")
            if not summary:
                raise RuntimeError(f"task '{task_id}' resumed without a summary")
            return self._complete_task(task_id, summary)

    def _complete_task(self, task_id: str, summary: str) -> Task:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status != TaskStatus.COMPLETED:
                now = utc_now()
                task.status = TaskStatus.COMPLETED
                task.result_summary = summary
                task.completed_at = now
                task.updated_at = now
                append_task_event(
                    session,
                    task=task,
                    event_type="task.completed",
                    aggregate_type="task",
                    aggregate_id=task.task_id,
                    source="langgraph",
                    payload={"status": TaskStatus.COMPLETED, "summary": summary},
                )
                session.commit()
                session.refresh(task)
            return task
