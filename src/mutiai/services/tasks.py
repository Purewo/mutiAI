"""Task creation, idempotency, and assignment planning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from mutiai.api.errors import ApiError
from mutiai.domain import OrganizationSpec
from mutiai.models import (
    Assignment,
    OrganizationSpecVersion,
    RuntimeExecution,
    Task,
)
from mutiai.models.task import (
    AssignmentKind,
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskOrchestrationMode,
    TaskStatus,
)
from mutiai.services.events import append_task_event
from mutiai.services.organizations import get_owned_organization


def get_owned_task(
    session: Session,
    *,
    task_id: str,
    owner_user_id: str,
) -> Task:
    task = session.scalar(
        select(Task).where(
            Task.task_id == task_id,
            Task.owner_user_id == owner_user_id,
        )
    )
    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found.")
    return task


def _request_hash(
    organization_id: str,
    request_text: str,
    orchestration_mode: str,
) -> str:
    canonical = json.dumps(
        {
            "organization_id": organization_id,
            "request": request_text,
            "orchestration_mode": orchestration_mode,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_request_hash(organization_id: str, request_text: str) -> str:
    canonical = json.dumps(
        {"organization_id": organization_id, "request": request_text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_task(
    session: Session,
    *,
    owner_user_id: str,
    organization_id: str,
    request_text: str,
    idempotency_key: str,
    orchestration_mode: TaskOrchestrationMode = TaskOrchestrationMode.LEGACY,
) -> tuple[Task, bool]:
    organization = get_owned_organization(
        session,
        organization_id=organization_id,
        owner_user_id=owner_user_id,
    )
    request_hash = _request_hash(
        organization_id,
        request_text,
        orchestration_mode,
    )
    existing = session.scalar(
        select(Task).where(
            Task.owner_user_id == owner_user_id,
            Task.organization_id == organization_id,
            Task.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        compatible_legacy_hash = (
            orchestration_mode == TaskOrchestrationMode.LEGACY
            and existing.request_hash == _legacy_request_hash(
                organization_id,
                request_text,
            )
        )
        if (
            existing.request_hash != request_hash
            and not compatible_legacy_hash
        ):
            raise ApiError(
                409,
                "TASK_IDEMPOTENCY_CONFLICT",
                "The idempotency key was already used for a different request.",
            )
        return existing, False

    if organization.current_published_version_id is None:
        raise ApiError(
            409,
            "ORGANIZATION_NOT_PUBLISHED",
            "Publish an organization version before creating a task.",
        )

    task = Task(
        owner_user_id=owner_user_id,
        organization_id=organization_id,
        organization_spec_version_id=organization.current_published_version_id,
        request_text=request_text,
        request_hash=request_hash,
        idempotency_key=idempotency_key,
        orchestration_mode=orchestration_mode,
        status=TaskStatus.CREATED,
    )
    session.add(task)
    session.flush()
    append_task_event(
        session,
        task=task,
        event_type="task.created",
        aggregate_type="task",
        aggregate_id=task.task_id,
        source="product",
        payload={
            "status": TaskStatus.CREATED,
            "organization_spec_version_id": task.organization_spec_version_id,
        },
    )
    session.commit()
    session.refresh(task)
    return task, True


def deterministic_assignment_id(
    task_id: str,
    role_key: str,
    *,
    assignment_key: str | None = None,
) -> str:
    identity = assignment_key or role_key
    return str(uuid5(NAMESPACE_URL, f"mutiai:assignment:{task_id}:{identity}"))


def deterministic_execution_id(
    task_id: str,
    role_key: str,
    *,
    assignment_key: str | None = None,
) -> str:
    identity = assignment_key or role_key
    return str(uuid5(NAMESPACE_URL, f"mutiai:execution:{task_id}:{identity}"))


def prepare_assignments(
    session: Session,
    *,
    task: Task,
    runtime_provider: str,
) -> list[Assignment]:
    existing = session.scalars(
        select(Assignment)
        .where(Assignment.task_id == task.task_id)
        .order_by(Assignment.agent_role_key)
    ).all()
    version = session.get(
        OrganizationSpecVersion,
        task.organization_spec_version_id,
    )
    if version is None:
        raise ApiError(
            409,
            "ORGANIZATION_VERSION_MISSING",
            "The task organization version is unavailable.",
        )
    spec = OrganizationSpec.model_validate(version.spec_payload)
    specialists = sorted(
        (role for role in spec.roles if not role.is_lead),
        key=lambda role: role.role_key,
    )
    if not specialists:
        raise ApiError(
            409,
            "ORGANIZATION_HAS_NO_SPECIALISTS",
            "The organization has no specialist role available for this task.",
        )

    specialist_keys = {role.role_key for role in specialists}
    existing_specialists = [
        assignment
        for assignment in existing
        if assignment.agent_role_key in specialist_keys
    ]
    if existing_specialists:
        if len(existing_specialists) != len(specialists):
            raise RuntimeError("task has an incomplete specialist assignment set")
        return sorted(existing_specialists, key=lambda item: item.agent_role_key)

    task.status = TaskStatus.PLANNING
    append_task_event(
        session,
        task=task,
        event_type="task.status_changed",
        aggregate_type="task",
        aggregate_id=task.task_id,
        source="langgraph",
        payload={"status": TaskStatus.PLANNING},
    )

    assignments: list[Assignment] = []
    for role in specialists:
        assignment = Assignment(
            assignment_id=deterministic_assignment_id(task.task_id, role.role_key),
            task_id=task.task_id,
            assignment_key=f"legacy.specialist:{role.role_key}",
            assignment_kind=AssignmentKind.LEGACY_SPECIALIST,
            agent_role_key=role.role_key,
            instructions=(
                f"Complete the task within this responsibility boundary: "
                f"{role.responsibility}\n\nTask: {task.request_text}"
            ),
            acceptance_criteria=(
                "Return a concise delivery summary for organization-lead review."
            ),
            execution_id=deterministic_execution_id(task.task_id, role.role_key),
            status=AssignmentStatus.SUBMITTED,
        )
        runtime_execution = RuntimeExecution(
            execution_id=assignment.execution_id,
            assignment_id=assignment.assignment_id,
            provider=runtime_provider,
            status=RuntimeExecutionStatus.SUBMITTED,
        )
        session.add_all([assignment, runtime_execution])
        session.flush()
        append_task_event(
            session,
            task=task,
            event_type="assignment.created",
            aggregate_type="assignment",
            aggregate_id=assignment.assignment_id,
            assignment_id=assignment.assignment_id,
            source="langgraph",
            payload={
                "agent_role_key": role.role_key,
                "status": AssignmentStatus.SUBMITTED,
            },
        )
        append_task_event(
            session,
            task=task,
            event_type="runtime.execution_submitted",
            aggregate_type="runtime_execution",
            aggregate_id=runtime_execution.runtime_execution_id,
            assignment_id=assignment.assignment_id,
            runtime_execution_id=runtime_execution.runtime_execution_id,
            source=f"runtime.{runtime_provider}",
            payload={
                "execution_id": assignment.execution_id,
                "provider": runtime_provider,
                "status": RuntimeExecutionStatus.SUBMITTED,
            },
        )
        assignments.append(assignment)

    task.status = TaskStatus.RUNNING
    append_task_event(
        session,
        task=task,
        event_type="task.status_changed",
        aggregate_type="task",
        aggregate_id=task.task_id,
        source="langgraph",
        payload={"status": TaskStatus.RUNNING},
    )
    session.commit()
    return assignments


def prepare_lead_plan(
    session: Session,
    *,
    task: Task,
    runtime_provider: str,
) -> Assignment:
    """Create the durable planning Assignment for a planned Task."""

    if task.orchestration_mode != TaskOrchestrationMode.PLANNED:
        raise ApiError(
            409,
            "TASK_NOT_PLANNED",
            "Only a planned Task can create a lead planning Assignment.",
        )
    version = session.get(
        OrganizationSpecVersion,
        task.organization_spec_version_id,
    )
    if version is None:
        raise ApiError(
            409,
            "ORGANIZATION_VERSION_MISSING",
            "The task organization version is unavailable.",
        )
    spec = OrganizationSpec.model_validate(version.spec_payload)
    lead = next(role for role in spec.roles if role.is_lead)
    assignment_key = "lead.plan"
    instructions = (
        f"Act as the organization lead within this responsibility boundary: "
        f"{lead.responsibility}\n\n"
        "Design a strict linear execution plan for the original request. "
        "Use only the already published organization roles below. Do not add "
        "or persist new formal roles, do not execute specialist work, and do "
        "not modify project files. Return only JSON matching the supplied "
        "TaskExecutionPlanSpec schema. The final step must be your lead_review. "
        "That lead_review must consume the final specialist Artifact and set "
        "output_contracts to an empty array. It returns a review decision through "
        "the review response contract and must not declare a new Artifact.\n\n"
        f"Original request:\n{task.request_text}\n\n"
        f"Published OrganizationSpec:\n"
        f"{json.dumps(version.spec_payload, ensure_ascii=False, indent=2)}"
    )
    existing = session.scalar(
        select(Assignment).where(
            Assignment.task_id == task.task_id,
            Assignment.assignment_key == assignment_key,
        )
    )
    if existing is not None:
        if existing.agent_role_key != lead.role_key:
            raise RuntimeError("persisted lead plan assignment has the wrong role")
        if (
            existing.status in {AssignmentStatus.SUBMITTED, AssignmentStatus.RUNNING}
            and task.status != TaskStatus.PLANNING
        ):
            task.status = TaskStatus.PLANNING
            append_task_event(
                session,
                task=task,
                event_type="task.status_changed",
                aggregate_type="task",
                aggregate_id=task.task_id,
                source="langgraph",
                payload={"status": TaskStatus.PLANNING, "reason": "lead_plan"},
            )
            session.commit()
        return existing

    task.status = TaskStatus.PLANNING
    append_task_event(
        session,
        task=task,
        event_type="task.status_changed",
        aggregate_type="task",
        aggregate_id=task.task_id,
        source="langgraph",
        payload={"status": TaskStatus.PLANNING, "reason": "lead_plan"},
    )
    assignment = Assignment(
        assignment_id=deterministic_assignment_id(
            task.task_id,
            lead.role_key,
            assignment_key=assignment_key,
        ),
        task_id=task.task_id,
        assignment_key=assignment_key,
        assignment_kind=AssignmentKind.LEAD_PLAN,
        agent_role_key=lead.role_key,
        instructions=instructions,
        acceptance_criteria=(
            "Return only a valid TaskExecutionPlanSpec using existing roles, "
            "with a strict linear sequence ending in lead_review whose "
            "output_contracts is empty."
        ),
        execution_id=deterministic_execution_id(
            task.task_id,
            lead.role_key,
            assignment_key=assignment_key,
        ),
        status=AssignmentStatus.SUBMITTED,
    )
    runtime_execution = RuntimeExecution(
        execution_id=assignment.execution_id,
        assignment_id=assignment.assignment_id,
        provider=runtime_provider,
        status=RuntimeExecutionStatus.SUBMITTED,
    )
    session.add_all([assignment, runtime_execution])
    session.flush()
    append_task_event(
        session,
        task=task,
        event_type="assignment.created",
        aggregate_type="assignment",
        aggregate_id=assignment.assignment_id,
        assignment_id=assignment.assignment_id,
        source="langgraph",
        payload={
            "agent_role_key": lead.role_key,
            "assignment_key": assignment_key,
            "assignment_kind": AssignmentKind.LEAD_PLAN,
            "status": AssignmentStatus.SUBMITTED,
        },
    )
    append_task_event(
        session,
        task=task,
        event_type="runtime.execution_submitted",
        aggregate_type="runtime_execution",
        aggregate_id=runtime_execution.runtime_execution_id,
        assignment_id=assignment.assignment_id,
        runtime_execution_id=runtime_execution.runtime_execution_id,
        source=f"runtime.{runtime_provider}",
        payload={
            "execution_id": assignment.execution_id,
            "provider": runtime_provider,
            "status": RuntimeExecutionStatus.SUBMITTED,
            "assignment_kind": AssignmentKind.LEAD_PLAN,
        },
    )
    session.commit()
    session.refresh(assignment)
    return assignment


def prepare_lead_review(
    session: Session,
    *,
    task: Task,
    specialist_results: Sequence[Mapping[str, str]],
    runtime_provider: str,
) -> Assignment:
    """Create or return the durable organization-lead review assignment."""

    version = session.get(
        OrganizationSpecVersion,
        task.organization_spec_version_id,
    )
    if version is None:
        raise ApiError(
            409,
            "ORGANIZATION_VERSION_MISSING",
            "The task organization version is unavailable.",
        )
    spec = OrganizationSpec.model_validate(version.spec_payload)
    lead = next(role for role in spec.roles if role.is_lead)
    specialist_keys = {role.role_key for role in spec.roles if not role.is_lead}
    result_by_role: dict[str, dict[str, str]] = {}
    for result in specialist_results:
        role_key = result["role_key"]
        if role_key in result_by_role:
            raise RuntimeError(f"lead review contains duplicate role '{role_key}'")
        result_by_role[role_key] = {
            "assignment_id": result["assignment_id"],
            "execution_id": result["execution_id"],
            "role_key": role_key,
            "summary": result["summary"],
        }
    if set(result_by_role) != specialist_keys:
        raise RuntimeError("lead review does not contain every specialist result")

    review_packet = {
        "task_id": task.task_id,
        "organization_spec_version_id": task.organization_spec_version_id,
        "original_request": task.request_text,
        "specialist_deliveries": [
            result_by_role[role_key] for role_key in sorted(result_by_role)
        ],
    }
    instructions = (
        f"Act as the organization lead within this responsibility boundary: "
        f"{lead.responsibility}\n\n"
        "Review the original request and every specialist delivery below. "
        "Do not invent missing work or silently fix a specialist's result. "
        "Return decision='accepted' only when the combined delivery satisfies "
        "the request. Otherwise return decision='needs_revision', explain the "
        "issues, and provide a concise user-facing final_summary.\n\n"
        f"Review packet:\n{json.dumps(review_packet, ensure_ascii=False, indent=2)}"
    )
    existing = session.scalar(
        select(Assignment).where(
            Assignment.task_id == task.task_id,
            Assignment.assignment_key == "legacy.lead_review",
        )
    )
    if existing is None:
        existing = session.scalar(
            select(Assignment).where(
                Assignment.task_id == task.task_id,
                Assignment.agent_role_key == lead.role_key,
                Assignment.assignment_kind == AssignmentKind.LEGACY,
            )
        )
        if existing is not None:
            existing.assignment_key = "legacy.lead_review"
            existing.assignment_kind = AssignmentKind.LEGACY_LEAD_REVIEW
            session.commit()
    if existing is not None:
        if existing.instructions != instructions:
            raise RuntimeError("persisted lead review instructions do not match")
        return existing

    assignment = Assignment(
        assignment_id=deterministic_assignment_id(task.task_id, lead.role_key),
        task_id=task.task_id,
        assignment_key="legacy.lead_review",
        assignment_kind=AssignmentKind.LEGACY_LEAD_REVIEW,
        agent_role_key=lead.role_key,
        instructions=instructions,
        acceptance_criteria=(
            "Return a structured accepted or needs_revision decision, issues, "
            "and the final user-facing delivery summary."
        ),
        execution_id=deterministic_execution_id(task.task_id, lead.role_key),
        status=AssignmentStatus.SUBMITTED,
    )
    runtime_execution = RuntimeExecution(
        execution_id=assignment.execution_id,
        assignment_id=assignment.assignment_id,
        provider=runtime_provider,
        status=RuntimeExecutionStatus.SUBMITTED,
    )
    session.add_all([assignment, runtime_execution])
    session.flush()
    append_task_event(
        session,
        task=task,
        event_type="assignment.created",
        aggregate_type="assignment",
        aggregate_id=assignment.assignment_id,
        assignment_id=assignment.assignment_id,
        source="langgraph",
        payload={
            "agent_role_key": lead.role_key,
            "assignment_kind": "lead_review",
            "status": AssignmentStatus.SUBMITTED,
        },
    )
    append_task_event(
        session,
        task=task,
        event_type="lead.review_requested",
        aggregate_type="assignment",
        aggregate_id=assignment.assignment_id,
        assignment_id=assignment.assignment_id,
        source="langgraph",
        payload={
            "agent_role_key": lead.role_key,
            "specialist_role_keys": sorted(result_by_role),
        },
    )
    append_task_event(
        session,
        task=task,
        event_type="runtime.execution_submitted",
        aggregate_type="runtime_execution",
        aggregate_id=runtime_execution.runtime_execution_id,
        assignment_id=assignment.assignment_id,
        runtime_execution_id=runtime_execution.runtime_execution_id,
        source=f"runtime.{runtime_provider}",
        payload={
            "execution_id": assignment.execution_id,
            "provider": runtime_provider,
            "status": RuntimeExecutionStatus.SUBMITTED,
        },
    )
    session.commit()
    session.refresh(assignment)
    return assignment
