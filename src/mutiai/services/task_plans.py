"""Persist and validate product-owned Task execution plans."""

from __future__ import annotations

import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.api.errors import ApiError
from mutiai.domain import OrganizationSpec, TaskExecutionPlanSpec
from mutiai.models import (
    OrganizationSpecVersion,
    PlanStep,
    PlanStepDependency,
    Task,
    TaskExecutionPlan,
)
from mutiai.models.base import utc_now
from mutiai.models.task_plan import PlanStepStatus, TaskExecutionPlanStatus
from mutiai.services.events import append_task_event


def plan_definition_hash(spec: TaskExecutionPlanSpec) -> str:
    payload = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deterministic_plan_id(task_id: str, plan_version: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"mutiai:task-plan:{task_id}:{plan_version}"))


def deterministic_plan_step_id(plan_id: str, step_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"mutiai:plan-step:{plan_id}:{step_key}"))


def deterministic_dependency_id(
    plan_id: str,
    step_key: str,
    depends_on: str,
) -> str:
    identity = f"mutiai:plan-dependency:{plan_id}:{step_key}:{depends_on}"
    return str(uuid5(NAMESPACE_URL, identity))


def validate_strict_linear_plan(
    *,
    plan_spec: TaskExecutionPlanSpec,
    organization_spec: OrganizationSpec,
) -> None:
    """Validate the M2.2 linear subset against a frozen organization version."""

    role_by_key = {role.role_key: role for role in organization_spec.roles}
    lead = next(role for role in organization_spec.roles if role.is_lead)
    used_roles: set[str] = set()

    if len(plan_spec.steps) < 2:
        raise ApiError(
            422,
            "TASK_PLAN_LINEAR_SHAPE_REQUIRED",
            "A linear plan requires at least one specialist step and lead review.",
        )

    for index, step in enumerate(plan_spec.steps):
        role = role_by_key.get(step.role_key)
        if role is None:
            raise ApiError(
                422,
                "TASK_PLAN_UNKNOWN_ROLE",
                f"Plan step '{step.step_key}' references an unknown role.",
            )
        if step.role_key in used_roles:
            raise ApiError(
                422,
                "TASK_PLAN_ROLE_REUSED",
                "M2.2 permits at most one plan step per role and Task.",
            )
        used_roles.add(step.role_key)

        expected_dependencies = () if index == 0 else (plan_spec.steps[index - 1].step_key,)
        if step.depends_on != expected_dependencies:
            raise ApiError(
                422,
                "TASK_PLAN_LINEAR_SHAPE_REQUIRED",
                f"Plan step '{step.step_key}' must depend only on the previous step.",
            )

        is_final = index == len(plan_spec.steps) - 1
        if is_final:
            if step.step_kind != "lead_review" or step.role_key != lead.role_key:
                raise ApiError(
                    422,
                    "TASK_PLAN_LEAD_REVIEW_REQUIRED",
                    "The final plan step must be owned by the organization lead as lead_review.",
                )
            if step.output_contracts:
                raise ApiError(
                    422,
                    "TASK_PLAN_LEAD_REVIEW_OUTPUT_FORBIDDEN",
                    "The lead review cannot replace specialist Artifact output.",
                )
        elif step.step_kind != "specialist" or role.is_lead:
            raise ApiError(
                422,
                "TASK_PLAN_SPECIALIST_STEP_INVALID",
                "Every step before lead review must be owned by a non-lead specialist.",
            )


def create_task_execution_plan(
    session: Session,
    *,
    task: Task,
    plan_spec: TaskExecutionPlanSpec,
    source: str,
    plan_version: int = 1,
) -> tuple[TaskExecutionPlan, bool]:
    """Persist an immutable strict-linear plan, or return its exact replay."""

    if not source or len(source) > 50:
        raise ValueError("Task plan source must contain 1 to 50 characters")
    if plan_version < 1:
        raise ValueError("Task plan version must be positive")

    version = session.get(OrganizationSpecVersion, task.organization_spec_version_id)
    if version is None:
        raise ApiError(
            409,
            "ORGANIZATION_VERSION_MISSING",
            "The Task organization version is unavailable.",
        )
    organization_spec = OrganizationSpec.model_validate(version.spec_payload)
    validate_strict_linear_plan(
        plan_spec=plan_spec,
        organization_spec=organization_spec,
    )

    definition_hash = plan_definition_hash(plan_spec)
    existing = session.scalar(
        select(TaskExecutionPlan).where(
            TaskExecutionPlan.task_id == task.task_id,
            TaskExecutionPlan.plan_version == plan_version,
        )
    )
    if existing is not None:
        if (
            existing.definition_hash != definition_hash
            or existing.organization_spec_version_id
            != task.organization_spec_version_id
            or existing.source != source
        ):
            raise ApiError(
                409,
                "TASK_PLAN_VERSION_CONFLICT",
                "The Task plan version already contains a different definition.",
            )
        return existing, False

    plan_id = deterministic_plan_id(task.task_id, plan_version)
    plan = TaskExecutionPlan(
        plan_id=plan_id,
        task_id=task.task_id,
        organization_spec_version_id=task.organization_spec_version_id,
        plan_version=plan_version,
        schema_version=plan_spec.schema_version,
        definition_hash=definition_hash,
        source=source,
        status=TaskExecutionPlanStatus.VALIDATED,
        summary=plan_spec.summary,
        validation_summary="Validated as a strict linear M2.2 plan.",
        initial_input_contracts=list(plan_spec.initial_input_contracts),
    )
    session.add(plan)
    session.flush()

    step_by_key: dict[str, PlanStep] = {}
    for sequence, step_spec in enumerate(plan_spec.steps):
        step = PlanStep(
            plan_step_id=deterministic_plan_step_id(plan_id, step_spec.step_key),
            plan_id=plan_id,
            step_key=step_spec.step_key,
            role_key=step_spec.role_key,
            step_kind=step_spec.step_kind,
            sequence=sequence,
            objective=step_spec.objective,
            acceptance_criteria=step_spec.acceptance_criteria,
            input_contracts=list(step_spec.input_contracts),
            output_contracts=[
                contract.model_dump(mode="json")
                for contract in step_spec.output_contracts
            ],
            status=(
                PlanStepStatus.READY
                if sequence == 0
                else PlanStepStatus.PENDING_DEPENDENCY
            ),
            ready_at=utc_now() if sequence == 0 else None,
        )
        session.add(step)
        step_by_key[step_spec.step_key] = step
    session.flush()

    for step_spec in plan_spec.steps:
        for dependency_key in step_spec.depends_on:
            session.add(
                PlanStepDependency(
                    dependency_id=deterministic_dependency_id(
                        plan_id,
                        step_spec.step_key,
                        dependency_key,
                    ),
                    plan_step_id=step_by_key[step_spec.step_key].plan_step_id,
                    depends_on_step_id=step_by_key[dependency_key].plan_step_id,
                )
            )

    append_task_event(
        session,
        task=task,
        event_type="task.execution_plan_created",
        aggregate_type="task_execution_plan",
        aggregate_id=plan.plan_id,
        source="product",
        payload={
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "definition_hash": plan.definition_hash,
            "status": plan.status,
            "step_count": len(plan_spec.steps),
        },
    )
    first_step = step_by_key[plan_spec.steps[0].step_key]
    append_task_event(
        session,
        task=task,
        event_type="plan.step_ready",
        aggregate_type="plan_step",
        aggregate_id=first_step.plan_step_id,
        source="product",
        payload={
            "plan_id": plan.plan_id,
            "plan_step_id": first_step.plan_step_id,
            "step_key": first_step.step_key,
            "role_key": first_step.role_key,
            "status": first_step.status,
        },
    )
    session.commit()
    session.refresh(plan)
    return plan, True
