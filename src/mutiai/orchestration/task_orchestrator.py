"""Product-owned task runner backed by a replaceable LangGraph graph."""

from __future__ import annotations

from pathlib import Path
from threading import RLock

from langgraph.checkpoint.sqlite import SqliteSaver
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


class TaskOrchestrator:
    """Runs the M1 graph while keeping durable facts in the product database."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime_adapter: AgentRuntimeAdapter | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
        self._execution_lock = RLock()

    def run(self, task_id: str) -> Task:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status == TaskStatus.COMPLETED:
                return task
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
                "assignments": [self._work(assignment) for assignment in assignments],
                "results": [],
                "summary": "",
            }

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

        return self._complete_task(task_id, result["summary"])

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

                completed_at = utc_now()
                execution.status = RuntimeExecutionStatus.COMPLETED
                execution.runtime_job_id = runtime_result.runtime_job_id
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
