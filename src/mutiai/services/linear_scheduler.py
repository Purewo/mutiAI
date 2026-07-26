"""Dependency scheduler for supported linear and parallel planned Tasks."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy import select

from mutiai.db import Database
from mutiai.domain import (
    ArtifactContractSpec,
    AssignmentDelivery,
    LeadReviewExecutionEvidence,
    LeadReviewResult,
    OrganizationSpec,
    ReviewArtifactEvidence,
    ReviewAssignmentEvidence,
    ReviewEvidenceChecks,
    ReviewInputBindingEvidence,
    ReviewPlanEvidence,
    ReviewRuntimeEvidence,
    ReviewStepEvidence,
    ReviewStepTargetEvidence,
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
    AssignmentKind,
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskStatus,
)
from mutiai.models.task_plan import PlanStepStatus, TaskExecutionPlanStatus
from mutiai.services.artifacts import ArtifactError, ArtifactManager
from mutiai.services.events import append_task_event
from mutiai.services.workspaces import WorkspaceProvisioner


class LinearTaskScheduler:
    """Prepare and finalize supported product-owned plan steps."""

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

    def is_parallel_plan(self, task_id: str) -> bool:
        with self.database.session() as session:
            plan = self._current_plan(session, task_id)
            if plan is None:
                return False
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan.plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            specialists = [
                step for step in steps if step.step_kind == "specialist"
            ]
            review = next(
                (step for step in steps if step.step_kind == "lead_review"),
                None,
            )
            if len(specialists) < 2 or review is None:
                return False
            if any(step.dependencies for step in specialists):
                return False
            review_dependencies = {
                dependency.depends_on_step_id for dependency in review.dependencies
            }
            return review_dependencies == {
                step.plan_step_id for step in specialists
            }

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

            self._activate_plan(session, task=task, plan=plan)

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

                assignment = self._create_assignment(
                    session,
                    task=task,
                    plan=plan,
                    step=step,
                )
                session.commit()
                return self._work_for_step(assignment, step)
            return None

    def prepare_parallel_specialists(
        self,
        task_id: str,
    ) -> list[dict[str, Any]]:
        """Create or restore every unfinished root specialist Assignment."""

        with self.database.session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task '{task_id}' does not exist")
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.NEEDS_REVISION,
                TaskStatus.CANCELLED,
            }:
                return []
            plan = self._current_plan(session, task_id)
            if plan is None:
                raise RuntimeError(f"task '{task_id}' has no execution plan")
            self._activate_plan(session, task=task, plan=plan)
            steps = session.scalars(
                select(PlanStep)
                .where(
                    PlanStep.plan_id == plan.plan_id,
                    PlanStep.step_kind == "specialist",
                )
                .order_by(PlanStep.sequence)
            ).all()
            work: list[dict[str, Any]] = []
            for step in steps:
                if step.status == PlanStepStatus.COMPLETED:
                    continue
                if step.dependencies:
                    raise RuntimeError(
                        "parallel specialist steps cannot declare dependencies"
                    )
                if step.status in {
                    PlanStepStatus.BLOCKED,
                    PlanStepStatus.FAILED,
                    PlanStepStatus.CANCELLED,
                }:
                    return []
                assignment = session.scalar(
                    select(Assignment).where(
                        Assignment.plan_step_id == step.plan_step_id
                    )
                )
                if assignment is None:
                    assignment = self._create_assignment(
                        session,
                        task=task,
                        plan=plan,
                        step=step,
                    )
                work.append(self._work_for_step(assignment, step))
            session.commit()
            return work

    def finalize_parallel_specialists(
        self,
        task_id: str,
        work: list[dict[str, Any]],
        results: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Publish one parallel wave after every Runtime branch completes."""

        result_by_assignment = {
            result["assignment_id"]: result for result in results
        }
        review: dict[str, Any] | None = None
        failures: list[str] = []
        for assignment_work in work:
            result = result_by_assignment.get(assignment_work["assignment_id"])
            if result is None:
                raise RuntimeError(
                    "parallel specialist wave completed without every result"
                )
            try:
                outcome = self.finalize_step(task_id, assignment_work, result)
            except RuntimeError as exc:
                # Finalize every branch before ending the fan-in. One invalid
                # delivery must not leave a sibling in validating_output.
                failures.append(str(exc))
                continue
            if outcome.get("review"):
                review = outcome["review"]
        if failures:
            self._cancel_unreachable_steps(
                task_id,
                reason="parallel_sibling_failed",
            )
            return {"review": None, "terminal": True}
        return {"review": review, "terminal": False}

    def _cancel_unreachable_steps(self, task_id: str, *, reason: str) -> None:
        """Move plan steps that can no longer run to a terminal state."""

        terminal_statuses = {
            PlanStepStatus.COMPLETED,
            PlanStepStatus.BLOCKED,
            PlanStepStatus.FAILED,
            PlanStepStatus.CANCELLED,
        }
        with self.database.session() as session:
            task = session.get(Task, task_id)
            plan = self._current_plan(session, task_id)
            if task is None or plan is None:
                raise LookupError("failed parallel plan records are unavailable")
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan.plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            cancelled_at = utc_now()
            for step in steps:
                if step.status in terminal_statuses:
                    continue
                step.status = PlanStepStatus.CANCELLED
                step.completed_at = cancelled_at
                append_task_event(
                    session,
                    task=task,
                    event_type="plan.step_cancelled",
                    aggregate_type="plan_step",
                    aggregate_id=step.plan_step_id,
                    source="langgraph",
                    payload={
                        "plan_step_id": step.plan_step_id,
                        "step_key": step.step_key,
                        "status": step.status,
                        "reason": reason,
                    },
                )
            session.commit()

    @staticmethod
    def _activate_plan(session, *, task: Task, plan: TaskExecutionPlan) -> None:
        if plan.status != TaskExecutionPlanStatus.VALIDATED:
            return
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

    def _create_assignment(
        self,
        session,
        *,
        task: Task,
        plan: TaskExecutionPlan,
        step: PlanStep,
    ) -> Assignment:
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
            plan=plan,
            step=step,
            bindings=bindings,
        )
        assignment = Assignment(
            assignment_id=self._assignment_id(task.task_id, step.step_key),
            task_id=task.task_id,
            assignment_key=f"plan_step:{step.step_key}",
            assignment_kind=AssignmentKind.PLAN_STEP,
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
        return assignment

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
        plan: TaskExecutionPlan,
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
            packet["execution_evidence"] = self._build_review_execution_evidence(
                session,
                task=task,
                plan=plan,
                review_step=step,
            )
            packet["review_rule"] = (
                "Inspect the supplied final Artifact against the original request "
                "and acceptance criteria. Treat execution_evidence as the product's "
                "authoritative record of the frozen plan, dependency order, input "
                "bindings, Assignment ownership, and Artifact validation. Do not "
                "require Codex transcripts, hidden reasoning, or upstream Artifact "
                "bytes that are not in materialized_inputs. Do not repair specialist "
                "files, invent missing work, or claim OS-level access prevention "
                "beyond the attestation_scope."
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
    def _duration_seconds(
        started_at: datetime | None,
        completed_at: datetime | None,
    ) -> float | None:
        if started_at is None or completed_at is None:
            return None
        return round(max(0.0, (completed_at - started_at).total_seconds()), 6)

    @staticmethod
    def _review_artifact_evidence(artifact: Artifact) -> ReviewArtifactEvidence:
        if artifact.status != "released":
            raise RuntimeError(
                f"review evidence requires a released Artifact, got "
                f"'{artifact.status}' for '{artifact.artifact_id}'"
            )
        return ReviewArtifactEvidence(
            artifact_id=artifact.artifact_id,
            contract_key=artifact.contract_key,
            origin=artifact.origin,
            producer_assignment_id=artifact.producer_assignment_id,
            producer_plan_step_id=artifact.producer_plan_step_id,
            schema_version=artifact.schema_version,
            media_type=artifact.media_type,
            sha256=artifact.sha256,
            byte_size=artifact.byte_size,
            status="released",
            validation_summary=artifact.validation_summary or "",
        )

    @classmethod
    def _review_input_evidence(
        cls,
        session,
        bindings: list[ArtifactInputBinding],
    ) -> tuple[ReviewInputBindingEvidence, ...]:
        evidence: list[ReviewInputBindingEvidence] = []
        for binding in bindings:
            artifact = session.get(Artifact, binding.artifact_id)
            if artifact is None:
                raise RuntimeError(
                    f"review evidence input Artifact '{binding.artifact_id}' "
                    "is unavailable"
                )
            if binding.status != "materialized":
                raise RuntimeError(
                    f"review evidence requires a materialized input binding, got "
                    f"'{binding.status}' for '{binding.input_binding_id}'"
                )
            evidence.append(
                ReviewInputBindingEvidence(
                    input_binding_id=binding.input_binding_id,
                    status="materialized",
                    artifact_sha256=binding.artifact_sha256,
                    artifact=cls._review_artifact_evidence(artifact),
                )
            )
        return tuple(evidence)

    @classmethod
    def _review_assignment_evidence(
        cls,
        assignment: Assignment,
    ) -> ReviewAssignmentEvidence:
        if assignment.status != AssignmentStatus.COMPLETED:
            raise RuntimeError(
                f"review evidence requires completed Assignment "
                f"'{assignment.assignment_id}'"
            )
        execution = assignment.runtime_execution
        if execution is None or execution.status != RuntimeExecutionStatus.COMPLETED:
            raise RuntimeError(
                f"review evidence requires completed RuntimeExecution for "
                f"'{assignment.assignment_id}'"
            )
        return ReviewAssignmentEvidence(
            assignment_id=assignment.assignment_id,
            assignment_key=assignment.assignment_key,
            assignment_kind=assignment.assignment_kind,
            role_key=assignment.agent_role_key,
            status="completed",
            runtime=ReviewRuntimeEvidence(
                runtime_execution_id=execution.runtime_execution_id,
                execution_id=execution.execution_id,
                provider=execution.provider,
                status="completed",
                requested_model=execution.requested_model,
                actual_model=execution.actual_model,
                reasoning_effort=execution.reasoning_effort,
                security_mode=execution.security_mode,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                run_duration_seconds=cls._duration_seconds(
                    execution.started_at,
                    execution.completed_at,
                ),
            ),
        )

    @classmethod
    def _build_review_execution_evidence(
        cls,
        session,
        *,
        task: Task,
        plan: TaskExecutionPlan,
        review_step: PlanStep,
    ) -> dict[str, Any]:
        """Build bounded product evidence before submitting lead.review."""

        if plan.status != TaskExecutionPlanStatus.ACTIVE:
            raise RuntimeError(
                "lead review evidence requires an active execution plan"
            )
        all_steps = session.scalars(
            select(PlanStep)
            .where(PlanStep.plan_id == plan.plan_id)
            .order_by(PlanStep.sequence)
        ).all()
        if not all_steps or all_steps[-1].plan_step_id != review_step.plan_step_id:
            raise RuntimeError("lead review must be the final execution plan step")

        planning_assignment = session.scalar(
            select(Assignment).where(
                Assignment.task_id == task.task_id,
                Assignment.assignment_kind == AssignmentKind.LEAD_PLAN,
            )
        )
        if planning_assignment is None:
            raise RuntimeError("lead planning Assignment is unavailable")
        planning_evidence = cls._review_assignment_evidence(planning_assignment)

        step_by_id = {step.plan_step_id: step for step in all_steps}
        step_by_key = {step.step_key: step for step in all_steps}
        specialist_evidence: list[ReviewStepEvidence] = []
        for step in all_steps[:-1]:
            if step.step_kind != "specialist" or step.status != PlanStepStatus.COMPLETED:
                raise RuntimeError(
                    f"review evidence found incomplete specialist step "
                    f"'{step.step_key}'"
                )
            assignment = session.scalar(
                select(Assignment).where(Assignment.plan_step_id == step.plan_step_id)
            )
            if assignment is None:
                raise RuntimeError(
                    f"review evidence is missing Assignment for '{step.step_key}'"
                )
            assignment_evidence = cls._review_assignment_evidence(assignment)
            input_bindings = session.scalars(
                select(ArtifactInputBinding)
                .where(ArtifactInputBinding.plan_step_id == step.plan_step_id)
                .order_by(ArtifactInputBinding.created_at)
            ).all()
            input_evidence = cls._review_input_evidence(session, input_bindings)
            declared_inputs = tuple(step.input_contracts)
            actual_inputs = tuple(
                item.artifact.contract_key for item in input_evidence
            )
            if Counter(declared_inputs) != Counter(actual_inputs):
                raise RuntimeError(
                    f"review evidence input contracts do not match '{step.step_key}'"
                )
            output_contracts = tuple(
                str(contract["contract_key"]) for contract in step.output_contracts
            )
            outputs = session.scalars(
                select(Artifact)
                .where(
                    Artifact.task_id == task.task_id,
                    Artifact.producer_plan_step_id == step.plan_step_id,
                    Artifact.status == "released",
                )
                .order_by(Artifact.created_at)
            ).all()
            output_evidence = tuple(
                cls._review_artifact_evidence(artifact) for artifact in outputs
            )
            actual_outputs = tuple(item.contract_key for item in output_evidence)
            if Counter(output_contracts) != Counter(actual_outputs):
                raise RuntimeError(
                    f"review evidence outputs do not match '{step.step_key}'"
                )
            dependency_keys = tuple(
                step_by_id[dependency.depends_on_step_id].step_key
                for dependency in step.dependencies
            )
            if any(
                step_by_key[key].status != PlanStepStatus.COMPLETED
                for key in dependency_keys
            ):
                raise RuntimeError(
                    f"review evidence found incomplete dependency for '{step.step_key}'"
                )
            specialist_evidence.append(
                ReviewStepEvidence(
                    plan_step_id=step.plan_step_id,
                    step_key=step.step_key,
                    role_key=step.role_key,
                    sequence=step.sequence,
                    status="completed",
                    depends_on_step_keys=dependency_keys,
                    declared_input_contracts=declared_inputs,
                    materialized_inputs=input_evidence,
                    declared_output_contracts=output_contracts,
                    released_outputs=output_evidence,
                    assignment=assignment_evidence,
                )
            )

        review_bindings = session.scalars(
            select(ArtifactInputBinding)
            .where(ArtifactInputBinding.plan_step_id == review_step.plan_step_id)
            .order_by(ArtifactInputBinding.created_at)
        ).all()
        review_inputs = cls._review_input_evidence(session, review_bindings)
        if Counter(review_step.input_contracts) != Counter(
            item.artifact.contract_key for item in review_inputs
        ):
            raise RuntimeError("review evidence final input contracts do not match")
        review_dependencies = tuple(
            step_by_id[dependency.depends_on_step_id].step_key
            for dependency in review_step.dependencies
        )
        return LeadReviewExecutionEvidence(
            task_id=task.task_id,
            plan=ReviewPlanEvidence(
                plan_id=plan.plan_id,
                plan_version=plan.plan_version,
                definition_hash=plan.definition_hash,
                source=plan.source,
                status="active",
                validation_summary=plan.validation_summary or "",
            ),
            planning_assignment=planning_evidence,
            specialist_steps=tuple(specialist_evidence),
            review_step=ReviewStepTargetEvidence(
                plan_step_id=review_step.plan_step_id,
                step_key=review_step.step_key,
                role_key=review_step.role_key,
                depends_on_step_keys=review_dependencies,
                declared_input_contracts=tuple(review_step.input_contracts),
                materialized_inputs=review_inputs,
            ),
            checks=ReviewEvidenceChecks(
                planning_assignment_completed=True,
                predecessor_steps_completed=True,
                dependency_order_satisfied=True,
                input_bindings_match_declared_contracts=True,
                outputs_match_declared_contracts=True,
                artifacts_released_and_validated=True,
            ),
            attestation_scope=(
                "The product attests to the frozen plan dependencies, Assignment "
                "ownership and status, materialized Artifact bindings, and released "
                "Artifact validation recorded here. This evidence contains no Codex "
                "transcripts or hidden reasoning and does not claim OS-level prevention "
                "of every undeclared read under demo_full_access."
            ),
        ).model_dump(mode="json")

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
        failure_summary = (
            f"Product delivery validation failed ({reason}): {error[:1000]}"
        )
        step.status = PlanStepStatus.FAILED
        plan.status = TaskExecutionPlanStatus.FAILED
        assignment.status = AssignmentStatus.FAILED
        assignment.result_summary = failure_summary
        execution = assignment.runtime_execution
        if execution is not None:
            # The Provider execution may have completed successfully while its
            # product delivery failed validation. Preserve that layered status,
            # but do not expose a misleading success summary on the failed work.
            execution.result_summary = failure_summary
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
                "summary": failure_summary,
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
