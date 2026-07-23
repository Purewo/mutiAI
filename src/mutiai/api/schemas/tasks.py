"""Task, assignment, Runtime execution, and event API contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from mutiai.api.schemas.organizations import as_utc
from mutiai.models import Assignment, ProductEvent, RuntimeExecution, Task
from mutiai.models.task import (
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskStatus,
)


class TaskCreateRequest(BaseModel):
    request: str = Field(min_length=1, max_length=10_000)


class RuntimeExecutionResponse(BaseModel):
    runtime_execution_id: str
    execution_id: str
    provider: str
    status: RuntimeExecutionStatus
    runtime_job_id: str | None
    thread_id: str | None
    turn_id: str | None
    workspace_id: str | None
    result_summary: str | None

    @classmethod
    def from_record(cls, execution: RuntimeExecution) -> "RuntimeExecutionResponse":
        return cls(
            runtime_execution_id=execution.runtime_execution_id,
            execution_id=execution.execution_id,
            provider=execution.provider,
            status=RuntimeExecutionStatus(execution.status),
            runtime_job_id=execution.runtime_job_id,
            thread_id=execution.thread_id,
            turn_id=execution.turn_id,
            workspace_id=execution.workspace_id,
            result_summary=execution.result_summary,
        )


class AssignmentResponse(BaseModel):
    assignment_id: str
    agent_role_key: str
    instructions: str
    acceptance_criteria: str
    execution_id: str
    status: AssignmentStatus
    result_summary: str | None
    runtime_execution: RuntimeExecutionResponse | None

    @classmethod
    def from_record(cls, assignment: Assignment) -> "AssignmentResponse":
        return cls(
            assignment_id=assignment.assignment_id,
            agent_role_key=assignment.agent_role_key,
            instructions=assignment.instructions,
            acceptance_criteria=assignment.acceptance_criteria,
            execution_id=assignment.execution_id,
            status=AssignmentStatus(assignment.status),
            result_summary=assignment.result_summary,
            runtime_execution=(
                RuntimeExecutionResponse.from_record(assignment.runtime_execution)
                if assignment.runtime_execution is not None
                else None
            ),
        )


class TaskResponse(BaseModel):
    task_id: str
    organization_id: str
    organization_spec_version_id: str
    request: str
    status: TaskStatus
    result_summary: str | None
    assignments: list[AssignmentResponse]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_record(cls, task: Task) -> "TaskResponse":
        return cls(
            task_id=task.task_id,
            organization_id=task.organization_id,
            organization_spec_version_id=task.organization_spec_version_id,
            request=task.request_text,
            status=TaskStatus(task.status),
            result_summary=task.result_summary,
            assignments=[
                AssignmentResponse.from_record(assignment)
                for assignment in task.assignments
            ],
            created_at=as_utc(task.created_at),
            updated_at=as_utc(task.updated_at),
            completed_at=as_utc(task.completed_at),
        )


class TaskEventResponse(BaseModel):
    event_id: str
    event_type: str
    schema_version: str
    aggregate_type: str
    aggregate_id: str
    task_id: str
    assignment_id: str | None
    runtime_execution_id: str | None
    sequence: int
    occurred_at: datetime
    source: str
    correlation_id: str
    payload: dict

    @classmethod
    def from_record(cls, event: ProductEvent) -> "TaskEventResponse":
        return cls(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            task_id=event.task_id,
            assignment_id=event.assignment_id,
            runtime_execution_id=event.runtime_execution_id,
            sequence=event.sequence,
            occurred_at=as_utc(event.occurred_at),
            source=event.source,
            correlation_id=event.correlation_id,
            payload=event.payload,
        )
