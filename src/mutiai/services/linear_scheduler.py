"""Dependency-driven scheduler for the strict-linear M2.2 plan subset."""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy import select

from mutiai.db import Database
from mutiai.domain import (
    ArtifactContractSpec,
    AssignmentDelivery,
    LeadReviewResult,
    OrganizationSpec,
)
from mutiai.models import (
    Artifact,
    ArtifactInputBinding,
    Assignment,
    OrganizationSpecVersion,
    PlanStep,
    PlanStepDependency,
    RuntimeExecution,
    Task,
    TaskExecutionPlan,
)
from mutiai.models.base import utc_now
from mutiai.models.task import (
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskStatus,
)
from mutiai.models.task_plan import PlanStepStatus, TaskExecutionPlanStatus
from mutiai.services.artifacts import ArtifactError, ArtifactManager
from mutiai.services.events import append_task_event
from mutiai.services.workspaces import WorkspaceProvisioner


class LinearTaskScheduler:
    """Prepare one ready Assignment and finalize one validated delivery at a time."""

    def __init__(
        self,
        database: Database,
        *,
        runtime_provider: str,
        workspace_provisioner: WorkspaceProvisioner,
    ) -> None:
        self.database = database
        self.runtime_provider = runtime_provider
        self.workspace_provisioner = workspace_provisioner
        self.artifact_manager = ArtifactManager(workspace_provisioner.manager)

    def has_plan(self, task_id: str) -> bool:
        with self.database.session() as session:
            return (
                session.scalar(
                    select(TaskExecutionPlan.plan_id)
                    .where(TaskExecutionPlan.task_id == task_id)
                    .limit(1)
                )
                is not None
            )

    def prepare_step(self, task_id: str) -> dict[str, Any] | None:
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
                return None
            plan = self._current_plan(session, task_id)
            if plan is None:
                raise RuntimeError(f"task '{task_id}' has no execution plan")

            if plan.status == TaskExecutionPlanStatus.VALIDATED:
                plan.status = TaskExecutionPlanStatus.ACTIVE
                plan.activated_at = plan.activated_at or utc_now()
                append_task_event(
                    session,
                    task=task,
                    event_type="task.execution_plan_activated",
                    aggregate_type="task_execution_plan",
                    aggregate_id=plan.plan_id,
                    source="langgraph",
                    payload={
                        "plan_id": plan.plan_id,
                        "plan_version": plan.plan_version,
                        "status": plan.status,
                    },
                )

            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan.plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            for step in steps:
                if step.status == PlanStepStatus.COMPLETED:
                    continue
                if step.status in {
                    PlanStepStatus.BLOCKED,
                    PlanStepStatus.FAILED,
                    PlanStepStatus.CANCELLED,
                }:
                    return None

                assignment = session.scalar(
                    select(Assignment).where(
                        Assignment.plan_step_id == step.plan_step_id
                    )
                )
                if assignment is not None:
                    return self._work_for_step(assignment, step)

                dependency_statuses = session.scalars(
                    select(PlanStep.status)
                    .join(
                        PlanStepDependency,
                        PlanStep.plan_step_id
                        == PlanStepDependency.depends_on_step_id,
                    )
                    .where(
                        PlanStepDependency.plan_step_id == step.plan_step_id
                    )
                ).all()
                if dependency_statuses and any(
                    status != PlanStepStatus.COMPLETED
                    for status in dependency_statuses
                ):
                    return None

                if step.status != PlanStepStatus.READY:
                    step.status = PlanStepStatus.READY
                    step.ready_at = utc_now()
                    append_task_event(
                        session,
                        task=task,
                        event_type="plan.step_ready",
                        aggregate_type="plan_step",
                        aggregate_id=step.plan_step_id,
                        source="langgraph",
                        payload={
                            "plan_id": plan.plan_id,
                            "plan_step_id": step.plan_step_id,
                            "step_key": step.step_key,
                            "role_key": step.role_key,
                            "status": step.status,
                        },
                    )

                workspace = self.workspace_provisioner.ensure_role_workspace(
                    session,
                    owner_user_id=task.owner_user_id,
                    organization_id=task.organization_id,
                    agent_role_key=step.role_key,
                    runtime_provider=self.runtime_provider,
                )
                bindings = self.artifact_manager.materialize_step_inputs(
                    session,
                    task=task,
                    plan_step=step,
                    consumer_workspace=workspace,
                )
                instructions = self._build_instructions(
                    session,
                    task=task,
                    step=step,
                    bindings=bindings,
                )
                assignment = Assignment(
                    assignment_id=self._assignment_id(task.task_id, step.step_key),
                    task_id=task.task_id,
                    agent_role_key=step.role_key,
                    instructions=instructions,
                    acceptance_criteria=step.acceptance_criteria,
                    execution_id=self._execution_id(task.task_id, step.step_key),
                    plan_step_id=step.plan_step_id,
                    status=AssignmentStatus.SUBMITTED,
                )
                execution = RuntimeExecution(
                    execution_id=assignment.execution_id,
                    assignment_id=assignment.assignment_id,
                    provider=self.runtime_provider,
                    workspace_id=workspace.workspace_id,
                    status=RuntimeExecutionStatus.SUBMITTED,
                )
                session.add_all([assignment, execution])
                session.flush()
                step.status = PlanStepStatus.SUBMITTED
                task.status = TaskStatus.RUNNING
                task.updated_at = utc_now()
                append_task_event(
                    session,
                    task=task,
                    event_type="assignment.created",
                    aggregate_type="assignment",
                    aggregate_id=assignment.assignment_id,
                    assignment_id=assignment.assignment_id,
                    source="langgraph",
                    payload={
                        "agent_role_key": step.role_key,
                        "plan_step_id": step.plan_step_id,
                        "step_key": step.step_key,
                        "step_kind": step.step_kind,
                        "status": assignment.status,
                    },
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="runtime.execution_submitted",
                    aggregate_type="runtime_execution",
                    aggregate_id=execution.runtime_execution_id,
                    assignment_id=assignment.assignment_id,
                    runtime_execution_id=execution.runtime_execution_id,
                    source=f"runtime.{self.runtime_provider}",
                    payload={
                        "execution_id": execution.execution_id,
                        "provider": execution.provider,
                        "plan_step_id": step.plan_step_id,
                        "workspace_id": workspace.workspace_id,
                        "status": execution.status,
                    },
                )
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
                        "status": step.status,
                    },
                )
                session.commit()
                return self._work_for_step(assignment, step)
            return None

    def finalize_step(
        self,
        task_id: str,
        work: dict[str, Any],
        result: dict[str, str],
    ) -> dict[str, Any]:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            assignment = session.get(Assignment, work["assignment_id"])
            if task is None or assignment is None or assignment.plan_step_id is None:
                raise LookupError("linear plan completion records are unavailable")
            step = session.get(PlanStep, assignment.plan_step_id)
            plan = self._current_plan(session, task_id)
            if step is None or plan is None:
                raise LookupError("linear plan step is unavailable")
            if task.status == TaskStatus.CANCELLED:
                return {"done": True, "review": None}
            if step.status == PlanStepStatus.COMPLETED:
                if step.step_kind == "lead_review":
                    review = LeadReviewResult.model_validate_json(result["summary"])
                    return {
                        "done": True,
                        "review": review.model_dump(mode="json"),
                    }
                return {"done": False, "review": None}

            if step.step_kind == "lead_review":
                try:
                    review = LeadReviewResult.model_validate_json(result["summary"])
                except (ValidationError, ValueError) as exc:
                    self._fail_step(
                        session,
                        task=task,
                        plan=plan,
                        step=step,
                        assignment=assignment,
                        reason="invalid_lead_review",
                        error=str(exc),
                    )
                    raise RuntimeError(
                        "organization lead returned an invalid review"
                    ) from exc
                self._complete_step(
                    session,
                    task=task,
                    step=step,
                    assignment=assignment,
                    payload={"review": review.model_dump(mode="json")},
                )
                plan.status = (
                    TaskExecutionPlanStatus.COMPLETED
                    if review.decision == "accepted"
                    else TaskExecutionPlanStatus.NEEDS_REVISION
                )
                plan.completed_at = (
                    utc_now() if review.decision == "accepted" else None
                )
                append_task_event(
                    session,
                    task=task,
                    event_type="lead.review_completed",
                    aggregate_type="assignment",
                    aggregate_id=assignment.assignment_id,
                    assignment_id=assignment.assignment_id,
                    source=f"runtime.{self.runtime_provider}",
                    payload=review.model_dump(mode="json"),
                )
                session.commit()
                return {
                    "done": True,
                    "review": review.model_dump(mode="json"),
                }

            try:
                delivery = AssignmentDelivery.model_validate_json(result["summary"])
            except (ValidationError, ValueError) as exc:
                self._fail_step(
                    session,
                    task=task,
                    plan=plan,
                    step=step,
                    assignment=assignment,
                    reason="invalid_assignment_delivery",
                    error=str(exc),
                )
                raise RuntimeError("specialist returned an invalid delivery") from exc

            if delivery.status == "blocked":
                step.status = PlanStepStatus.BLOCKED
                plan.status = TaskExecutionPlanStatus.NEEDS_REVISION
                assignment.status = AssignmentStatus.FAILED
                append_task_event(
                    session,
                    task=task,
                    event_type="plan.step_blocked",
                    aggregate_type="plan_step",
                    aggregate_id=step.plan_step_id,
                    assignment_id=assignment.assignment_id,
                    source=f"runtime.{self.runtime_provider}",
                    payload={
                        "plan_step_id": step.plan_step_id,
                        "step_key": step.step_key,
                        "summary": delivery.summary,
                        "status": step.status,
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
                        "status": assignment.status,
                        "reason": "plan_step_blocked",
                    },
                )
                session.commit()
                return {
                    "done": True,
                    "review": {
                        "decision": "needs_revision",
                        "final_summary": delivery.summary,
                        "issues": [f"Plan step '{step.step_key}' is blocked."],
                    },
                }

            execution = assignment.runtime_execution
            if execution is None:
                raise LookupError("linear plan Runtime execution is unavailable")
            delivery_id = (
                execution.runtime_event_id
                or execution.turn_id
                or execution.runtime_job_id
                or execution.execution_id
            )
            try:
                artifacts = self.artifact_manager.publish_assignment_delivery(
                    session,
                    task=task,
                    assignment=assignment,
                    delivery=delivery,
                    source_delivery_id=delivery_id,
                )
            except ArtifactError as exc:
                self._fail_step_after_artifact_rollback(
                    task_id=task.task_id,
                    plan_step_id=step.plan_step_id,
                    assignment_id=assignment.assignment_id,
                    reason=exc.code.lower(),
                    error=exc.message,
                )
                raise RuntimeError("specialist Artifact validation failed") from exc

            self._complete_step(
                session,
                task=task,
                step=step,
                assignment=assignment,
                payload={
                    "delivery_summary": delivery.summary,
                    "artifact_ids": [artifact.artifact_id for artifact in artifacts],
                },
            )
            session.commit()
            return {"done": False, "review": None}

    def _build_instructions(
        self,
        session,
        *,
        task: Task,
        step: PlanStep,
        bindings: tuple[ArtifactInputBinding, ...],
    ) -> str:
        version = session.get(
            OrganizationSpecVersion,
            task.organization_spec_version_id,
        )
        if version is None:
            raise RuntimeError("Task OrganizationSpec version is unavailable")
        organization_spec = OrganizationSpec.model_validate(version.spec_payload)
        role = next(item for item in organization_spec.roles if item.role_key == step.role_key)
        inputs = []
        for binding in bindings:
            artifact = session.get(Artifact, binding.artifact_id)
            if artifact is None:
                raise RuntimeError("materialized input Artifact is unavailable")
            inputs.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "contract_key": artifact.contract_key,
                    "schema_version": artifact.schema_version,
                    "media_type": artifact.media_type,
                    "relative_path": binding.materialized_relative_path,
                    "sha256": artifact.sha256,
                    "byte_size": artifact.byte_size,
                }
            )
        outputs = [
            ArtifactContractSpec.model_validate(payload).model_dump(mode="json")
            for payload in step.output_contracts
        ]
        packet: dict[str, Any] = {
            "plan_step_id": step.plan_step_id,
            "step_key": step.step_key,
            "step_kind": step.step_kind,
            "role_key": step.role_key,
            "responsibility_boundary": role.responsibility,
            "objective": step.objective,
            "acceptance_criteria": step.acceptance_criteria,
            "materialized_inputs": inputs,
            "required_outputs": outputs,
        }
        if step.step_kind == "lead_review":
            packet["original_user_request"] = task.request_text
            packet["review_rule"] = (
                "Inspect the delivered Artifacts and report deficiencies. "
                "Do not repair specialist files or invent missing work."
            )
        else:
            packet["delivery_rule"] = (
                "Work only from materialized_inputs. Write each required output "
                "inside this Workspace and return the structured delivery envelope."
            )
        return (
            "Execute this frozen product Assignment. Do not expand your formal role, "
            "adopt another role's Workspace, or perform another step's responsibility.\n\n"
            f"Assignment packet:\n{json.dumps(packet, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _work_for_step(assignment: Assignment, step: PlanStep) -> dict[str, Any]:
        output_schema = (
            LeadReviewResult.model_json_schema()
            if step.step_kind == "lead_review"
            else AssignmentDelivery.model_json_schema()
        )
        return {
            "assignment_id": assignment.assignment_id,
            "execution_id": assignment.execution_id,
            "role_key": assignment.agent_role_key,
            "instructions": assignment.instructions,
            "output_schema": output_schema,
        }

    @staticmethod
    def _current_plan(session, task_id: str) -> TaskExecutionPlan | None:
        return session.scalar(
            select(TaskExecutionPlan)
            .where(TaskExecutionPlan.task_id == task_id)
            .order_by(TaskExecutionPlan.plan_version.desc())
            .limit(1)
        )

    @staticmethod
    def _complete_step(
        session,
        *,
        task: Task,
        step: PlanStep,
        assignment: Assignment,
        payload: dict[str, Any],
    ) -> None:
        step.status = PlanStepStatus.COMPLETED
        step.completed_at = utc_now()
        task.updated_at = utc_now()
        append_task_event(
            session,
            task=task,
            event_type="plan.step_completed",
            aggregate_type="plan_step",
            aggregate_id=step.plan_step_id,
            assignment_id=assignment.assignment_id,
            source="langgraph",
            payload={
                "plan_step_id": step.plan_step_id,
                "step_key": step.step_key,
                "role_key": step.role_key,
                "status": step.status,
                **payload,
            },
        )

    @staticmethod
    def _fail_step(
        session,
        *,
        task: Task,
        plan: TaskExecutionPlan,
        step: PlanStep,
        assignment: Assignment,
        reason: str,
        error: str,
    ) -> None:
        step.status = PlanStepStatus.FAILED
        plan.status = TaskExecutionPlanStatus.FAILED
        assignment.status = AssignmentStatus.FAILED
        task.status = TaskStatus.FAILED
        task.updated_at = utc_now()
        append_task_event(
            session,
            task=task,
            event_type="plan.step_failed",
            aggregate_type="plan_step",
            aggregate_id=step.plan_step_id,
            assignment_id=assignment.assignment_id,
            source="langgraph",
            payload={
                "plan_step_id": step.plan_step_id,
                "step_key": step.step_key,
                "status": step.status,
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
                "status": assignment.status,
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
                "status": task.status,
                "reason": reason,
                "plan_step_id": step.plan_step_id,
            },
        )
        session.commit()

    def _fail_step_after_artifact_rollback(
        self,
        *,
        task_id: str,
        plan_step_id: str,
        assignment_id: str,
        reason: str,
        error: str,
    ) -> None:
        with self.database.session() as session:
            task = session.get(Task, task_id)
            step = session.get(PlanStep, plan_step_id)
            assignment = session.get(Assignment, assignment_id)
            plan = self._current_plan(session, task_id)
            if task is None or step is None or assignment is None or plan is None:
                raise LookupError("Artifact failure plan records are unavailable")
            self._fail_step(
                session,
                task=task,
                plan=plan,
                step=step,
                assignment=assignment,
                reason=reason,
                error=error,
            )

    @staticmethod
    def _assignment_id(task_id: str, step_key: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"mutiai:assignment:{task_id}:{step_key}"))

    @staticmethod
    def _execution_id(task_id: str, step_key: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"mutiai:execution:{task_id}:{step_key}"))
