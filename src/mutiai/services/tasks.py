"""Task creation, idempotency, and assignment planning."""

from __future__ import annotations

import hashlib
import json
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
    AssignmentStatus,
    RuntimeExecutionStatus,
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


def _request_hash(organization_id: str, request_text: str) -> str:
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
) -> tuple[Task, bool]:
    organization = get_owned_organization(
        session,
        organization_id=organization_id,
        owner_user_id=owner_user_id,
    )
    request_hash = _request_hash(organization_id, request_text)
    existing = session.scalar(
        select(Task).where(
            Task.owner_user_id == owner_user_id,
            Task.organization_id == organization_id,
            Task.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash:
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


def deterministic_assignment_id(task_id: str, role_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"mutiai:assignment:{task_id}:{role_key}"))


def deterministic_execution_id(task_id: str, role_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"mutiai:execution:{task_id}:{role_key}"))


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
    if existing:
        return list(existing)

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

    assignments: list[Assignment] = []
    for role in specialists:
        assignment = Assignment(
            assignment_id=deterministic_assignment_id(task.task_id, role.role_key),
            task_id=task.task_id,
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
