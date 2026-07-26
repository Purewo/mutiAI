"""Product-owned task runner backed by a replaceable LangGraph graph."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.api.errors import ApiError
from mutiai.config import Settings
from mutiai.db import Database
from mutiai.domain import (
    ArtifactContractSpec,
    LeadReviewResult,
    OrganizationSpec,
    TaskExecutionPlanSpec,
    WorkloadRequirements,
)
from mutiai.models import (
    Artifact,
    Assignment,
    OrganizationSpecVersion,
    PlanStep,
    ProductEvent,
    RuntimeExecution,
    Task,
    TaskExecutionPlan,
    Workspace,
)
from mutiai.models.approval import ApprovalRequest, ApprovalStatus
from mutiai.models.base import utc_now
from mutiai.models.task import (
    AssignmentKind,
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskOrchestrationMode,
    TaskStatus,
)
from mutiai.models.task_plan import (
    ArtifactStatus,
    PlanStepStatus,
    TaskExecutionPlanStatus,
)
from mutiai.orchestration.task_graph import (
    AssignmentResult,
    AssignmentWork,
    LeadReviewState,
    LinearTaskGraphState,
    ParallelTaskGraphState,
    PlanningGraphState,
    TaskGraphState,
    build_linear_task_graph,
    build_parallel_task_graph,
    build_planning_graph,
    build_task_graph,
)
from mutiai.runtime import (
    AgentRuntimeAdapter,
    FakeRuntimeAdapter,
    RuntimeExecutionConfig,
    RuntimeRecoveryRequest,
    RuntimeTokenUsage,
)
from mutiai.services.artifacts import ArtifactManager
from mutiai.services.events import append_task_event
from mutiai.services.feasibility import FeasibilityGateError, FeasibilityService
from mutiai.services.linear_scheduler import LinearTaskScheduler
from mutiai.services.runtime_bindings import (
    RuntimeBindingResolutionError,
    RuntimeBindingService,
)
from mutiai.services.runtime_controls import (
    CONCURRENCY_WAIT_REASON,
    RuntimeBudgetExceededError,
    RuntimeControlService,
    RuntimeProviderRateLimitedError,
)
from mutiai.services.task_plans import create_task_execution_plan
from mutiai.services.tasks import (
    prepare_assignments,
    prepare_lead_plan,
    prepare_lead_review,
)
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
        self.runtime_controls = RuntimeControlService(
            settings,
            self.runtime_adapter,
        )
        self.runtime_bindings = RuntimeBindingService(
            settings,
            runtime_provider=self.runtime_adapter.provider,
        )
        self.feasibility = FeasibilityService(settings, self.runtime_bindings)
        self.artifact_manager = (
            ArtifactManager(workspace_provisioner.manager)
            if workspace_provisioner is not None
            else None
        )
        self.linear_scheduler = (
            LinearTaskScheduler(
                database,
                runtime_provider=self.runtime_adapter.provider,
                workspace_provisioner=workspace_provisioner,
            )
            if workspace_provisioner is not None
            else None
        )

    def set_runtime_watch(self, watch: Callable[[str], None]) -> None:
        """Register the post-checkpoint Runtime completion watcher."""

        self._runtime_watch = watch

    def set_approval_canceller(
        self,
        cancel: Callable[..., list[str]],
    ) -> None:
        """Register the product-owned pending approval cancellation boundary."""

        self._approval_canceller = cancel

    def plan(self, task_id: str) -> Task:
        """Run or resume the durable organization-lead planning boundary."""

        with self._execution_lock:
            with self.database.session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    raise LookupError(f"task '{task_id}' does not exist")
                if task.orchestration_mode != TaskOrchestrationMode.PLANNED:
                    raise ValueError("only planned Tasks support a planning phase")
                if task.execution_plans:
                    assignment = session.scalar(
                        select(Assignment).where(
                            Assignment.task_id == task_id,
                            Assignment.assignment_key == "lead.plan",
                        )
                    )
                    if assignment is not None:
                        self._mark_planning_ready(
                            session,
                            task=task,
                            plan=task.execution_plans[-1],
                            assignment_id=assignment.assignment_id,
                        )
                    return task
                if task.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.NEEDS_REVISION,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    return task
                assignment = prepare_lead_plan(
                    session,
                    task=task,
                    runtime_provider=self.runtime_adapter.provider,
                )
                work = self._work(
                    assignment,
                    output_schema=TaskExecutionPlanSpec.model_json_schema(),
                )

            checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            config = {"configurable": {"thread_id": self._planning_thread_id(task_id)}}
            initial_state: PlanningGraphState = {
                "task_id": task_id,
                "work": work,
                "result": None,
            }
            with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                graph = build_planning_graph(self._execute_assignment).compile(
                    checkpointer=saver
                )
                snapshot = graph.get_state(config)
                if snapshot.next:
                    result = graph.invoke(None, config=config)
                elif (
                    snapshot.values
                    and snapshot.values.get("result")
                    and assignment.status == AssignmentStatus.COMPLETED
                ):
                    result = snapshot.values
                else:
                    result = graph.invoke(initial_state, config=config)

            with self.database.session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    raise LookupError(f"task '{task_id}' does not exist")
                if task.status == TaskStatus.WAITING:
                    self._watch_waiting_executions(task_id)
                    return task

            return self._complete_planning(task_id, result)

    def start(self, task_id: str) -> Task:
        """Start planned execution only after every declared input is released."""

        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.orchestration_mode != TaskOrchestrationMode.PLANNED:
                raise ValueError("only planned Tasks support an explicit start")
            plan = self._current_plan(session, task_id)
            if plan is None:
                raise ValueError("the Task must complete lead planning before start")
            missing = [
                contract_key
                for contract_key in plan.initial_input_contracts
                if not session.scalar(
                    select(Artifact.artifact_id).where(
                        Artifact.task_id == task_id,
                        Artifact.contract_key == contract_key,
                        Artifact.origin == "task_input",
                        Artifact.status == ArtifactStatus.RELEASED,
                    )
                )
            ]
            if missing:
                raise ValueError(
                    "Task inputs are missing: " + ", ".join(sorted(missing))
                )
        return self.run(task_id)

    def publish_task_input(
        self,
        *,
        task_id: str,
        contract_key: str,
        schema_version: str,
        media_type: str,
        file_name: str,
        content: bytes,
        source_delivery_id: str,
    ) -> Artifact:
        """Stage and publish one user input without exposing source paths."""

        if self.artifact_manager is None or self.workspace_provisioner is None:
            raise RuntimeError("Artifact input publishing requires Workspace support")
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("Task input content exceeds the 20 MiB limit")
        contract = ArtifactContractSpec(
            contract_key=contract_key,
            schema_version=schema_version,
            media_type=media_type,
            file_name=file_name,
        )
        staging_dir = self.workspace_provisioner.manager.provision(
            Path("staging") / task_id / str(uuid4())
        )
        staging_path = staging_dir / contract.file_name
        try:
            staging_path.write_bytes(content)
            with self.database.session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    raise LookupError(f"task '{task_id}' does not exist")
                if task.orchestration_mode != TaskOrchestrationMode.PLANNED:
                    raise ValueError("Task inputs are supported only for planned Tasks")
                plan = self._current_plan(session, task_id)
                if plan is None:
                    raise ValueError("the Task must complete lead planning first")
                artifact = self.artifact_manager.publish_task_input(
                    session,
                    task=task,
                    plan=plan,
                    contract=contract,
                    source_path=staging_path,
                    source_delivery_id=source_delivery_id,
                )
                return artifact
        finally:
            try:
                staging_path.unlink(missing_ok=True)
                staging_dir.rmdir()
            except OSError:
                pass

    def _complete_planning(self, task_id: str, result: dict[str, Any]) -> Task:
        planning_result = result.get("result") if result else None
        if not planning_result or not planning_result.get("summary"):
            raise RuntimeError(f"task '{task_id}' planning completed without output")
        try:
            plan_spec = TaskExecutionPlanSpec.model_validate_json(
                planning_result["summary"]
            )
        except (ValidationError, ValueError) as exc:
            self._record_planning_failure(task_id, str(exc))
            raise RuntimeError("organization lead returned an invalid execution plan") from exc
        try:
            with self._execution_lock, self.database.session() as session:
                task = session.get(Task, task_id)
                if task is None:
                    raise LookupError(f"task '{task_id}' does not exist")
                plan, _ = create_task_execution_plan(
                    session,
                    task=task,
                    plan_spec=plan_spec,
                    source="lead.plan",
                )
                self._mark_planning_ready(
                    session,
                    task=task,
                    plan=plan,
                    assignment_id=planning_result["assignment_id"],
                )
                return task
        except ApiError as exc:
            self._record_planning_failure(task_id, exc.message)
            raise

    def _mark_planning_ready(
        self,
        session: Session,
        *,
        task: Task,
        plan: TaskExecutionPlan,
        assignment_id: str,
    ) -> None:
        completed_event = session.scalar(
            select(ProductEvent).where(
                ProductEvent.task_id == task.task_id,
                ProductEvent.assignment_id == assignment_id,
                ProductEvent.event_type == "lead.plan_completed",
            )
        )
        if completed_event is None:
            append_task_event(
                session,
                task=task,
                event_type="lead.plan_completed",
                aggregate_type="assignment",
                aggregate_id=assignment_id,
                assignment_id=assignment_id,
                source=f"runtime.{self.runtime_adapter.provider}",
                payload={
                    "plan_id": plan.plan_id,
                    "definition_hash": plan.definition_hash,
                    "status": "ready_for_inputs",
                },
            )
        if (
            plan.status == TaskExecutionPlanStatus.VALIDATED
            and task.status in {TaskStatus.PLANNING, TaskStatus.WAITING}
        ):
            task.status = TaskStatus.CREATED
            task.updated_at = utc_now()
            append_task_event(
                session,
                task=task,
                event_type="task.status_changed",
                aggregate_type="task",
                aggregate_id=task.task_id,
                source="langgraph",
                payload={"status": TaskStatus.CREATED, "reason": "plan_ready"},
            )
        session.commit()
        session.refresh(task)

    def _record_planning_failure(self, task_id: str, error: str) -> None:
        with self._execution_lock, self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            assignment = session.scalar(
                select(Assignment).where(
                    Assignment.task_id == task_id,
                    Assignment.assignment_key == "lead.plan",
                )
            )
            if assignment is not None:
                assignment.status = AssignmentStatus.FAILED
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
                        "reason": "invalid_plan",
                    },
                )
            task.status = TaskStatus.FAILED
            task.result_summary = "Organization lead planning failed."
            task.updated_at = utc_now()
            append_task_event(
                session,
                task=task,
                event_type="task.failed",
                aggregate_type="task",
                aggregate_id=task.task_id,
                source="langgraph",
                payload={
                    "status": TaskStatus.FAILED,
                    "reason": "invalid_plan",
                    "error": error[:1000],
                },
            )
            session.commit()

    def run(self, task_id: str) -> Task:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            orchestration_mode = TaskOrchestrationMode(task.orchestration_mode)
        if orchestration_mode == TaskOrchestrationMode.PLANNED:
            if self.linear_scheduler is None:
                raise RuntimeError("planned execution requires Workspace provisioning")
            if not self.linear_scheduler.has_plan(task_id):
                return self.plan(task_id)
            return self._run_planned(task_id)
        if self.linear_scheduler is not None and self.linear_scheduler.has_plan(task_id):
            return self._run_planned(task_id)

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

    def _run_planned(self, task_id: str) -> Task:
        if self.linear_scheduler is None:
            raise RuntimeError("planned execution requires Workspace provisioning")
        if self.linear_scheduler.is_parallel_plan(task_id):
            return self._run_parallel(task_id)
        return self._run_linear(task_id)

    def _run_linear(self, task_id: str) -> Task:
        if self.linear_scheduler is None:
            raise RuntimeError("linear plan execution requires Workspace provisioning")

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
                self._watch_waiting_executions(task_id)
                return task

        initial_state: LinearTaskGraphState = {
            "task_id": task_id,
            "work": None,
            "result": None,
            "review": None,
            "done": False,
        }
        checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        config = {"configurable": {"thread_id": self._linear_thread_id(task_id)}}
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            graph = build_linear_task_graph(
                self.linear_scheduler.prepare_step,
                self._execute_assignment,
                self.linear_scheduler.finalize_step,
            ).compile(checkpointer=saver)
            snapshot = graph.get_state(config)
            if snapshot.next:
                result = graph.invoke(None, config=config)
            elif snapshot.values and snapshot.values.get("done"):
                result = snapshot.values
            else:
                result = graph.invoke(initial_state, config=config)

        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status == TaskStatus.WAITING:
                self._watch_waiting_executions(task_id)
                return task
            if task.status in {
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return task

        review = result.get("review")
        if review:
            return self._finish_task(task_id, review)
        raise RuntimeError(f"task '{task_id}' linear graph completed without review")

    def _run_parallel(self, task_id: str) -> Task:
        if self.linear_scheduler is None:
            raise RuntimeError("parallel plan execution requires Workspace provisioning")

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
                self._watch_waiting_executions(task_id)
                return task

        initial_state: ParallelTaskGraphState = {
            "task_id": task_id,
            "assignments": [],
            "results": [],
            "review_work": None,
            "review_result": None,
            "review": None,
        }
        checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        config = {"configurable": {"thread_id": self._parallel_thread_id(task_id)}}
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            graph = build_parallel_task_graph(
                self.linear_scheduler.prepare_parallel_specialists,
                self._execute_assignment,
                self.linear_scheduler.finalize_parallel_specialists,
                self.linear_scheduler.prepare_step,
                self.linear_scheduler.finalize_step,
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
                self._watch_waiting_executions(task_id)
                return task
            if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return task

        review = result.get("review")
        if review:
            return self._finish_task(task_id, review)
        raise RuntimeError(f"task '{task_id}' parallel graph completed without review")

    @staticmethod
    def _linear_thread_id(task_id: str) -> str:
        return f"{task_id}:linear"

    @staticmethod
    def _parallel_thread_id(task_id: str) -> str:
        return f"{task_id}:parallel"

    @staticmethod
    def _planning_thread_id(task_id: str) -> str:
        return f"{task_id}:planning"

    @staticmethod
    def _current_plan(
        session: Session,
        task_id: str,
    ) -> TaskExecutionPlan | None:
        return session.scalar(
            select(TaskExecutionPlan)
            .where(TaskExecutionPlan.task_id == task_id)
            .order_by(TaskExecutionPlan.plan_version.desc())
            .limit(1)
        )

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
                self._set_plan_step_status(
                    session,
                    task=task,
                    assignment=assignment,
                    status=PlanStepStatus.SUBMITTED,
                )
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
        self._resume_capacity_waiters_for_task(task_id)
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
        usage: RuntimeTokenUsage | None = None,
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
            terminal_usage=usage,
        )
        self._dispatch_runtime_cancellations(task_id, targets)
        self._cancel_pending_approvals(task_id)
        self._resume_capacity_waiters_for_task(task_id)
        return task

    def _resume_capacity_waiters_for_task(self, task_id: str) -> None:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            owner_user_id = task.owner_user_id
            providers = session.scalars(
                select(RuntimeExecution.provider)
                .join(Assignment)
                .where(Assignment.task_id == task_id)
                .distinct()
            ).all()
        for provider in providers:
            self._drain_capacity_waiters(
                owner_user_id=owner_user_id,
                provider=provider,
            )

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
        terminal_usage: RuntimeTokenUsage | None = None,
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
                        self.runtime_controls.settle(
                            session,
                            task=task,
                            execution=execution,
                            usage=(
                                terminal_usage
                                if execution.execution_id
                                == terminal_execution_id
                                else None
                            ),
                        )
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
                        and execution.wait_reason != CONCURRENCY_WAIT_REASON
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
                    RuntimeExecution.wait_reason.is_(None),
                    RuntimeExecution.turn_id.is_not(None),
                )
            ).all()
        for execution_id in execution_ids:
            self._runtime_watch(execution_id)

    def resume_deferred_runtime_executions(self) -> None:
        """Wake capacity-deferred executions after an application restart."""

        with self.database.session() as session:
            keys = session.execute(
                select(Task.owner_user_id, RuntimeExecution.provider)
                .join(Assignment, Assignment.task_id == Task.task_id)
                .join(
                    RuntimeExecution,
                    RuntimeExecution.assignment_id == Assignment.assignment_id,
                )
                .where(
                    Task.status == TaskStatus.WAITING,
                    RuntimeExecution.status == RuntimeExecutionStatus.WAITING,
                    RuntimeExecution.wait_reason == CONCURRENCY_WAIT_REASON,
                )
                .distinct()
            ).all()
        for owner_user_id, provider in keys:
            self._drain_capacity_waiters(
                owner_user_id=owner_user_id,
                provider=provider,
            )

    def _drain_capacity_waiters(self, *, owner_user_id: str, provider: str) -> None:
        """Resume the oldest queued branches whenever a Runtime slot is free."""

        while True:
            with self._execution_lock, self.database.session() as session:
                policy = self.runtime_controls.ensure_policy(
                    session,
                    owner_user_id=owner_user_id,
                    provider=provider,
                )
                active = self.runtime_controls.active_execution_count(
                    session,
                    owner_user_id=owner_user_id,
                    provider=provider,
                )
                if active >= policy.max_concurrent_executions:
                    return
                candidate = session.scalar(
                    select(RuntimeExecution)
                    .join(Assignment)
                    .join(Task)
                    .where(
                        Task.owner_user_id == owner_user_id,
                        Task.status == TaskStatus.WAITING,
                        Assignment.status == AssignmentStatus.WAITING,
                        RuntimeExecution.provider == provider,
                        RuntimeExecution.status == RuntimeExecutionStatus.WAITING,
                        RuntimeExecution.wait_reason == CONCURRENCY_WAIT_REASON,
                    )
                    .order_by(
                        RuntimeExecution.created_at,
                        RuntimeExecution.execution_id,
                    )
                )
                if candidate is None:
                    return
                assignment = session.get(Assignment, candidate.assignment_id)
                if assignment is None:
                    return
                task = session.get(Task, assignment.task_id)
                if task is None:
                    return
                candidate.status = RuntimeExecutionStatus.SUBMITTED
                candidate.wait_reason = None
                assignment.status = AssignmentStatus.SUBMITTED
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_capacity_available",
                    aggregate_type="runtime_execution",
                    aggregate_id=candidate.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=candidate.runtime_execution_id,
                    source="product",
                    payload={
                        "execution_id": candidate.execution_id,
                        "status": RuntimeExecutionStatus.SUBMITTED,
                        "reason": CONCURRENCY_WAIT_REASON,
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
                        "reason": CONCURRENCY_WAIT_REASON,
                    },
                )
                session.commit()
                task_id = task.task_id
                execution_id = candidate.execution_id

            try:
                self._resume_runtime_interrupt(
                    task_id=task_id,
                    execution_id=execution_id,
                    runtime_event_id=f"product:capacity:{execution_id}",
                )
            except (RuntimeProviderRateLimitedError, RuntimeBudgetExceededError):
                # Admission already persisted the explicit rejection. Continue
                # draining other queued work without sleeping in a graph node.
                continue
            except Exception as exc:  # noqa: BLE001 - wake-up boundary
                self.record_runtime_watch_error(
                    execution_id=execution_id,
                    error=str(exc)[:1000],
                )

    def complete_runtime_execution(
        self,
        *,
        execution_id: str,
        runtime_event_id: str,
        summary: str,
        runtime_job_id: str | None = None,
        last_event_position: str | None = None,
        usage: RuntimeTokenUsage | None = None,
        context_compactions: int = 0,
        actual_model: str | None = None,
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
                if assignment.assignment_kind == AssignmentKind.LEAD_PLAN:
                    should_resume = self._current_plan(session, task_id) is None
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
                execution.actual_model = actual_model or execution.actual_model
                execution.completed_at = completed_at
                self._record_workspace_completion(
                    session,
                    execution=execution,
                    summary=summary,
                    context_compactions=context_compactions,
                    completed_at=completed_at,
                )
                charged_tokens = self.runtime_controls.settle(
                    session,
                    task=task,
                    execution=execution,
                    usage=usage,
                )
                assignment.status = AssignmentStatus.COMPLETED
                assignment.result_summary = summary
                assignment.completed_at = completed_at
                self._set_plan_step_status(
                    session,
                    task=task,
                    assignment=assignment,
                    status=PlanStepStatus.VALIDATING_OUTPUT,
                )
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
                        "usage_status": execution.usage_status,
                        "total_tokens": execution.total_tokens,
                        "charged_tokens": charged_tokens,
                        "context_compactions": execution.context_compactions,
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
            owner_user_id = task.owner_user_id
            provider = execution.provider
            should_resume = should_resume and task.status not in {
                TaskStatus.FAILED,
                TaskStatus.COMPLETED,
                TaskStatus.NEEDS_REVISION,
                TaskStatus.CANCELLED,
            }

        if should_resume:
            task = self._resume_runtime_interrupt(
                task_id=task_id,
                execution_id=execution_id,
                runtime_event_id=runtime_event_id,
            )
        self._drain_capacity_waiters(
            owner_user_id=owner_user_id,
            provider=provider,
        )
        return task

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
        usage: RuntimeTokenUsage | None = None,
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
            charged_tokens = self.runtime_controls.settle(
                session,
                task=task,
                execution=execution,
                usage=usage,
            )
            execution.status = RuntimeExecutionStatus.FAILED
            execution.runtime_event_id = runtime_event_id
            execution.runtime_job_id = runtime_job_id or execution.runtime_job_id
            execution.thread_id = thread_id or execution.thread_id
            execution.turn_id = turn_id or execution.turn_id
            execution.completed_at = failed_at
            assignment.status = AssignmentStatus.FAILED
            assignment.completed_at = failed_at
            self._set_plan_step_status(
                session,
                task=task,
                assignment=assignment,
                status=PlanStepStatus.FAILED,
            )
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
                    "usage_status": execution.usage_status,
                    "total_tokens": execution.total_tokens,
                    "charged_tokens": charged_tokens,
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
            owner_user_id = task.owner_user_id
            provider = execution.provider

        self._drain_capacity_waiters(
            owner_user_id=owner_user_id,
            provider=provider,
        )
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
                    RuntimeExecution.runtime_binding_key,
                    RuntimeExecution.requested_model,
                    RuntimeExecution.reasoning_effort,
                    RuntimeExecution.security_mode,
                    RuntimeExecution.approval_policy,
                    RuntimeExecution.sandbox_mode,
                    RuntimeExecution.network_access,
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
                    RuntimeExecution.wait_reason.is_(None),
                    RuntimeExecution.thread_id.is_not(None),
                    RuntimeExecution.turn_id.is_not(None),
                )
            ).all()

        recovered: list[str] = []
        for (
            execution_id,
            runtime_job_id,
            thread_id,
            turn_id,
            workspace_id,
            runtime_binding_key,
            requested_model,
            reasoning_effort,
            security_mode,
            approval_policy,
            sandbox_mode,
            network_access,
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
                runtime_config = (
                    RuntimeExecutionConfig(
                        binding_key=runtime_binding_key,
                        model=requested_model,
                        reasoning_effort=reasoning_effort,
                        security_mode=security_mode,
                        approval_policy=approval_policy,
                        sandbox_mode=sandbox_mode,
                        network_access=network_access,
                    )
                    if runtime_binding_key is not None
                    and security_mode is not None
                    and approval_policy is not None
                    and sandbox_mode is not None
                    and network_access is not None
                    else None
                )
                try:
                    reattached = try_recover(
                        RuntimeRecoveryRequest(
                            execution_id=execution_id,
                            runtime_job_id=runtime_job_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            workspace_id=workspace_id,
                            workspace_path=workspace_path,
                            runtime_config=runtime_config,
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

    @staticmethod
    def _set_plan_step_status(
        session: Session,
        *,
        task: Task,
        assignment: Assignment,
        status: PlanStepStatus,
    ) -> None:
        if assignment.plan_step_id is None:
            return
        step = session.get(PlanStep, assignment.plan_step_id)
        if step is None:
            raise RuntimeError(
                f"assignment '{assignment.assignment_id}' has no plan step"
            )
        if step.status == status:
            return
        step.status = status
        if status == PlanStepStatus.FAILED:
            step.plan.status = TaskExecutionPlanStatus.FAILED
        elif (
            status == PlanStepStatus.SUBMITTED
            and step.plan.status == TaskExecutionPlanStatus.FAILED
        ):
            step.plan.status = TaskExecutionPlanStatus.ACTIVE
        append_task_event(
            session,
            task=task,
            event_type="plan.step_status_changed",
            aggregate_type="plan_step",
            aggregate_id=step.plan_step_id,
            assignment_id=assignment.assignment_id,
            source="langgraph",
            payload={
                "plan_step_id": step.plan_step_id,
                "step_key": step.step_key,
                "status": status,
            },
        )

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

    def _resolve_runtime_config(
        self,
        session: Session,
        *,
        task: Task,
        assignment: Assignment,
        execution: RuntimeExecution,
    ) -> RuntimeExecutionConfig:
        """Resolve once and persist the immutable policy used by an execution."""

        snapshot = self._runtime_config_snapshot(execution)
        if snapshot is not None:
            return snapshot

        version = session.get(
            OrganizationSpecVersion,
            task.organization_spec_version_id,
        )
        if version is None:
            raise RuntimeError(
                f"task '{task.task_id}' has no OrganizationSpec version"
            )
        spec = OrganizationSpec.model_validate(version.spec_payload)
        binding, config = self.runtime_bindings.resolve_for_role(
            session,
            owner_user_id=task.owner_user_id,
            spec=spec,
            role_key=assignment.agent_role_key,
        )
        execution.runtime_binding_id = binding.runtime_binding_id
        execution.runtime_binding_key = binding.binding_key
        execution.requested_model = config.model
        execution.reasoning_effort = config.reasoning_effort
        execution.security_mode = config.security_mode
        execution.approval_policy = config.approval_policy
        execution.sandbox_mode = config.sandbox_mode
        execution.network_access = config.network_access
        return config

    @staticmethod
    def _runtime_config_snapshot(
        execution: RuntimeExecution,
    ) -> RuntimeExecutionConfig | None:
        if execution.runtime_binding_key is None:
            return None
        required = (
            execution.security_mode,
            execution.approval_policy,
            execution.sandbox_mode,
            execution.network_access,
        )
        if any(value is None for value in required):
            raise RuntimeError(
                f"execution '{execution.execution_id}' has an incomplete "
                "Runtime configuration snapshot"
            )
        return RuntimeExecutionConfig(
            binding_key=execution.runtime_binding_key,
            model=execution.requested_model,
            reasoning_effort=execution.reasoning_effort,
            security_mode=execution.security_mode,
            approval_policy=execution.approval_policy,
            sandbox_mode=execution.sandbox_mode,
            network_access=execution.network_access,
        )

    @staticmethod
    def _record_workspace_completion(
        session: Session,
        *,
        execution: RuntimeExecution,
        summary: str,
        context_compactions: int,
        completed_at: datetime,
    ) -> None:
        """Persist compact Thread lifecycle facts without copying Codex history."""

        if context_compactions < 0:
            raise ValueError("context_compactions cannot be negative")
        workspace = (
            session.get(Workspace, execution.workspace_id)
            if execution.workspace_id is not None
            else None
        )
        execution.context_compactions = context_compactions
        if workspace is None:
            return
        workspace.last_delivery_summary = summary
        if context_compactions:
            workspace.thread_compaction_count += context_compactions
            workspace.last_compacted_at = completed_at

    def _rotate_thread_if_needed(
        self,
        session: Session,
        *,
        task: Task,
        execution: RuntimeExecution,
        workspace: Workspace,
    ) -> None:
        """Rotate only before a new execution after an explicit compaction limit."""

        threshold = self.settings.runtime_thread_max_compactions
        if (
            threshold is None
            or execution.thread_id is not None
            or workspace.codex_thread_id is None
            or workspace.thread_compaction_count < threshold
        ):
            return
        previous_thread_id = workspace.codex_thread_id
        workspace.codex_thread_id = None
        workspace.thread_compaction_count = 0
        workspace.thread_generation += 1
        append_task_event(
            session,
            task=task,
            event_type="runtime.thread_rotated",
            aggregate_type="workspace",
            aggregate_id=workspace.workspace_id,
            assignment_id=None,
            runtime_execution_id=execution.runtime_execution_id,
            source="product",
            payload={
                "execution_id": execution.execution_id,
                "workspace_id": workspace.workspace_id,
                "previous_thread_id": previous_thread_id,
                "thread_generation": workspace.thread_generation,
                "reason": "context_compaction_limit",
            },
        )

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
            resumed_from_capacity_wait = (
                execution.status == RuntimeExecutionStatus.WAITING
                and execution.wait_reason == CONCURRENCY_WAIT_REASON
            )
            if execution.status == RuntimeExecutionStatus.WAITING:
                if execution.wait_reason != CONCURRENCY_WAIT_REASON:
                    self._interrupt_for_runtime(task, assignment, execution)
                execution.status = RuntimeExecutionStatus.SUBMITTED
                execution.wait_reason = None
                assignment.status = AssignmentStatus.SUBMITTED

            try:
                runtime_config = self._resolve_runtime_config(
                    session,
                    task=task,
                    assignment=assignment,
                    execution=execution,
                )
            except RuntimeBindingResolutionError as exc:
                self._record_admission_failure(
                    session=session,
                    task=task,
                    assignment=assignment,
                    execution=execution,
                    error=exc,
                )
                raise

            version = session.get(
                OrganizationSpecVersion,
                task.organization_spec_version_id,
            )
            if version is None:
                raise RuntimeError(
                    f"task '{task.task_id}' has no OrganizationSpec version"
                )
            check = self.feasibility.evaluate_task_request(
                session,
                owner_user_id=task.owner_user_id,
                spec=OrganizationSpec.model_validate(version.spec_payload),
                request_text=task.request_text,
                explicit_requirements=WorkloadRequirements.model_validate(
                    task.capability_requirements or {}
                ),
                target_id=task.task_id,
                phase="runtime_start",
                role_key=assignment.agent_role_key,
            )
            try:
                self.feasibility.require_feasible(check)
            except FeasibilityGateError as exc:
                self._record_admission_failure(
                    session=session,
                    task=task,
                    assignment=assignment,
                    execution=execution,
                    error=exc,
                )
                raise

            try:
                admission = self.runtime_controls.admit(
                    session,
                    task=task,
                    execution=execution,
                )
            except (RuntimeProviderRateLimitedError, RuntimeBudgetExceededError) as exc:
                self._record_admission_failure(
                    session=session,
                    task=task,
                    assignment=assignment,
                    execution=execution,
                    error=exc,
                )
                raise
            if not admission.admitted:
                execution.status = RuntimeExecutionStatus.WAITING
                execution.wait_reason = CONCURRENCY_WAIT_REASON
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
                        source="product",
                        payload={
                            "status": TaskStatus.WAITING,
                            "reason": CONCURRENCY_WAIT_REASON,
                        },
                    )
                self._set_plan_step_status(
                    session,
                    task=task,
                    assignment=assignment,
                    status=PlanStepStatus.WAITING,
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_deferred",
                    aggregate_type="runtime_execution",
                    aggregate_id=execution.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=execution.runtime_execution_id,
                    source="product",
                    payload={
                        "execution_id": execution.execution_id,
                        "reason": CONCURRENCY_WAIT_REASON,
                        "active_executions": admission.active_executions,
                        "max_concurrent_executions": (
                            admission.max_concurrent_executions
                        ),
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
                    source="product",
                    payload={
                        "status": AssignmentStatus.WAITING,
                        "reason": CONCURRENCY_WAIT_REASON,
                    },
                )
                session.commit()
                self._interrupt_for_capacity(task, assignment, execution)

            if resumed_from_capacity_wait:
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_capacity_available",
                    aggregate_type="runtime_execution",
                    aggregate_id=execution.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=execution.runtime_execution_id,
                    source="product",
                    payload={
                        "execution_id": execution.execution_id,
                        "status": RuntimeExecutionStatus.SUBMITTED,
                        "reason": CONCURRENCY_WAIT_REASON,
                        "active_executions": admission.active_executions,
                        "max_concurrent_executions": (
                            admission.max_concurrent_executions
                        ),
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
                        "reason": CONCURRENCY_WAIT_REASON,
                    },
                )

            workspace = (
                session.get(Workspace, execution.workspace_id)
                if execution.workspace_id is not None
                else None
            )
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
                self._rotate_thread_if_needed(
                    session,
                    task=task,
                    execution=execution,
                    workspace=workspace,
                )

            now = utc_now()
            execution.status = RuntimeExecutionStatus.RUNNING
            execution.started_at = execution.started_at or now
            assignment.status = AssignmentStatus.RUNNING
            self._set_plan_step_status(
                session,
                task=task,
                assignment=assignment,
                status=PlanStepStatus.RUNNING,
            )
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
                    "reserved_tokens": admission.reserved_tokens,
                    "provider_capacity": (
                        admission.provider_capacity.status
                        if admission.provider_capacity is not None
                        else "unknown"
                    ),
                    "status": RuntimeExecutionStatus.RUNNING,
                    "runtime_binding_key": execution.runtime_binding_key,
                    "requested_model": execution.requested_model,
                    "reasoning_effort": execution.reasoning_effort,
                    "security_mode": execution.security_mode,
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
                instructions = work["instructions"]
                if (
                    workspace is not None
                    and workspace.codex_thread_id is None
                    and workspace.last_delivery_summary
                ):
                    instructions = (
                        "Previous Thread delivery summary for continuity:\n"
                        f"{workspace.last_delivery_summary}\n\n"
                        f"{instructions}"
                    )
                runtime_result = self.runtime_adapter.execute(
                    execution_id=work["execution_id"],
                    role_key=work["role_key"],
                    instructions=instructions,
                    workspace_id=workspace.workspace_id if workspace else None,
                    workspace_path=workspace.canonical_path if workspace else None,
                    thread_id=workspace.codex_thread_id if workspace else None,
                    output_schema=work["output_schema"],
                    runtime_config=runtime_config,
                )
            except Exception as exc:
                failed_at = utc_now()
                charged_tokens = self.runtime_controls.settle(
                    session,
                    task=task,
                    execution=execution,
                    usage=None,
                )
                execution.status = RuntimeExecutionStatus.FAILED
                assignment.status = AssignmentStatus.FAILED
                task.status = TaskStatus.FAILED
                task.updated_at = failed_at
                self._set_plan_step_status(
                    session,
                    task=task,
                    assignment=assignment,
                    status=PlanStepStatus.FAILED,
                )
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
                        "reason": getattr(
                            exc,
                            "reason",
                            "runtime_submission_failed",
                        ),
                        "error": str(exc)[:1000],
                        "charged_tokens": charged_tokens,
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
                        "reason": getattr(
                            exc,
                            "reason",
                            "runtime_submission_failed",
                        ),
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
                        "reason": getattr(
                            exc,
                            "reason",
                            "runtime_submission_failed",
                        ),
                    },
                )
                session.commit()
                raise

            if runtime_result.status == "waiting":
                execution.status = RuntimeExecutionStatus.WAITING
                execution.runtime_job_id = runtime_result.runtime_job_id
                execution.thread_id = runtime_result.thread_id
                execution.turn_id = runtime_result.turn_id
                execution.workspace_id = (
                    runtime_result.workspace_id or execution.workspace_id
                )
                execution.actual_model = runtime_result.actual_model
                execution.last_event_position = runtime_result.last_event_position
                execution.wait_reason = None
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
                self._set_plan_step_status(
                    session,
                    task=task,
                    assignment=assignment,
                    status=PlanStepStatus.WAITING,
                )
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
            charged_tokens = self.runtime_controls.settle(
                session,
                task=task,
                execution=execution,
                usage=runtime_result.usage,
            )
            execution.status = RuntimeExecutionStatus.COMPLETED
            execution.runtime_job_id = runtime_result.runtime_job_id
            execution.thread_id = runtime_result.thread_id
            execution.turn_id = runtime_result.turn_id
            execution.workspace_id = (
                runtime_result.workspace_id or execution.workspace_id
            )
            execution.actual_model = runtime_result.actual_model
            execution.last_event_position = runtime_result.last_event_position
            execution.result_summary = runtime_result.summary
            execution.completed_at = completed_at
            self._record_workspace_completion(
                session,
                execution=execution,
                summary=runtime_result.summary,
                context_compactions=runtime_result.context_compactions,
                completed_at=completed_at,
            )
            assignment.status = AssignmentStatus.COMPLETED
            assignment.result_summary = runtime_result.summary
            assignment.completed_at = completed_at
            self._set_plan_step_status(
                session,
                task=task,
                assignment=assignment,
                status=PlanStepStatus.VALIDATING_OUTPUT,
            )
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
                    "usage_status": execution.usage_status,
                    "total_tokens": execution.total_tokens,
                    "charged_tokens": charged_tokens,
                    "context_compactions": execution.context_compactions,
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
    def _interrupt_for_capacity(
        task: Task,
        assignment: Assignment,
        execution: RuntimeExecution,
    ) -> None:
        interrupt(
            {
                "kind": "runtime.capacity_waiting",
                "task_id": task.task_id,
                "assignment_id": assignment.assignment_id,
                "execution_id": execution.execution_id,
                "runtime_execution_id": execution.runtime_execution_id,
                "reason": CONCURRENCY_WAIT_REASON,
            }
        )
        raise RuntimeError(
            f"execution '{execution.execution_id}' is waiting for Runtime capacity"
        )

    def _record_admission_failure(
        self,
        *,
        session,
        task: Task,
        assignment: Assignment,
        execution: RuntimeExecution,
        error: RuntimeError,
    ) -> None:
        reason = getattr(error, "reason", None)
        if reason is None:
            reason = (
                "provider_rate_limited"
                if isinstance(error, RuntimeProviderRateLimitedError)
                else "runtime_budget_exceeded"
            )
        runtime_event_id = f"product:admission:{execution.execution_id}:{reason}"
        execution.status = RuntimeExecutionStatus.FAILED
        execution.runtime_event_id = runtime_event_id
        execution.completed_at = utc_now()
        assignment.status = AssignmentStatus.FAILED
        assignment.completed_at = execution.completed_at
        task.status = TaskStatus.FAILED
        task.result_summary = None
        task.completed_at = None
        task.updated_at = execution.completed_at
        payload = {
            "execution_id": execution.execution_id,
            "runtime_event_id": runtime_event_id,
            "status": RuntimeExecutionStatus.FAILED,
            "reason": reason,
            "error": str(error)[:1000],
        }
        if isinstance(error, RuntimeProviderRateLimitedError):
            payload["provider"] = error.provider
            payload["resets_at"] = error.resets_at
        if isinstance(error, RuntimeBudgetExceededError):
            payload.update(
                {
                    "provider": error.provider,
                    "budget_limit": error.limit,
                    "tokens_consumed": error.consumed,
                    "tokens_reserved": error.reserved,
                    "requested_tokens": error.requested,
                }
            )
        append_task_event(
            session,
            task=task,
            event_type="runtime.execution_rejected",
            aggregate_type="runtime_execution",
            aggregate_id=execution.runtime_execution_id,
            assignment_id=assignment.assignment_id,
            runtime_execution_id=execution.runtime_execution_id,
            source="product",
            payload=payload,
        )
        append_task_event(
            session,
            task=task,
            event_type="assignment.status_changed",
            aggregate_type="assignment",
            aggregate_id=assignment.assignment_id,
            assignment_id=assignment.assignment_id,
            source="product",
            payload={"status": AssignmentStatus.FAILED, "reason": reason},
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

            has_execution_plan = (
                self.linear_scheduler is not None
                and self.linear_scheduler.has_plan(task_id)
            )
            is_parallel = (
                has_execution_plan
                and self.linear_scheduler is not None
                and self.linear_scheduler.is_parallel_plan(task_id)
            )
            is_linear = has_execution_plan and not is_parallel
            is_planning = (
                not has_execution_plan
                and task.orchestration_mode == TaskOrchestrationMode.PLANNED
            )
            checkpoint_path = Path(self.settings.langgraph_checkpoint_path)
            config = {
                "configurable": {
                    "thread_id": (
                        self._parallel_thread_id(task_id)
                        if is_parallel
                        else self._linear_thread_id(task_id)
                        if is_linear
                        else self._planning_thread_id(task_id)
                        if is_planning
                        else task_id
                    )
                }
            }
            with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                if is_parallel:
                    if self.linear_scheduler is None:
                        raise RuntimeError("parallel scheduler is unavailable")
                    graph = build_parallel_task_graph(
                        self.linear_scheduler.prepare_parallel_specialists,
                        self._execute_assignment,
                        self.linear_scheduler.finalize_parallel_specialists,
                        self.linear_scheduler.prepare_step,
                        self.linear_scheduler.finalize_step,
                    ).compile(checkpointer=saver)
                elif is_linear:
                    if self.linear_scheduler is None:
                        raise RuntimeError("linear scheduler is unavailable")
                    graph = build_linear_task_graph(
                        self.linear_scheduler.prepare_step,
                        self._execute_assignment,
                        self.linear_scheduler.finalize_step,
                    ).compile(checkpointer=saver)
                elif is_planning:
                    graph = build_planning_graph(
                        self._execute_assignment,
                    ).compile(checkpointer=saver)
                else:
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
                elif snapshot.values and (
                    snapshot.values.get("review")
                    or (is_linear and snapshot.values.get("done"))
                    or (is_planning and snapshot.values.get("result"))
                ):
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
            if review:
                return self._finish_task(task_id, review)
            if is_planning and result.get("result"):
                return self._complete_planning(task_id, result)
            if has_execution_plan:
                with self.database.session() as session:
                    task = session.get(Task, task_id)
                    if task is None:
                        raise LookupError(f"task '{task_id}' does not exist")
                    if task.status in {
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    }:
                        return task
            raise RuntimeError(f"task '{task_id}' resumed without a review")

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
