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
    runtime_binding_id: str | None
    runtime_binding_key: str | None
    requested_model: str | None
    actual_model: str | None
    reasoning_effort: str | None
    security_mode: str | None
    approval_policy: str | None
    sandbox_mode: str | None
    network_access: bool | None
    status: RuntimeExecutionStatus
    runtime_job_id: str | None
    thread_id: str | None
    turn_id: str | None
    workspace_id: str | None
    result_summary: str | None
    wait_reason: str | None
    reserved_tokens: int
    charged_tokens: int | None
    usage_status: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    context_compactions: int

    @classmethod
    def from_record(cls, execution: RuntimeExecution) -> RuntimeExecutionResponse:
        return cls(
            runtime_execution_id=execution.runtime_execution_id,
            execution_id=execution.execution_id,
            provider=execution.provider,
            runtime_binding_id=execution.runtime_binding_id,
            runtime_binding_key=execution.runtime_binding_key,
            requested_model=execution.requested_model,
            actual_model=execution.actual_model,
            reasoning_effort=execution.reasoning_effort,
            security_mode=execution.security_mode,
            approval_policy=execution.approval_policy,
            sandbox_mode=execution.sandbox_mode,
            network_access=execution.network_access,
            status=RuntimeExecutionStatus(execution.status),
            runtime_job_id=execution.runtime_job_id,
            thread_id=execution.thread_id,
            turn_id=execution.turn_id,
            workspace_id=execution.workspace_id,
            result_summary=execution.result_summary,
            wait_reason=execution.wait_reason,
            reserved_tokens=execution.reserved_tokens,
            charged_tokens=execution.charged_tokens,
            usage_status=execution.usage_status,
            input_tokens=execution.input_tokens,
            cached_input_tokens=execution.cached_input_tokens,
            output_tokens=execution.output_tokens,
            reasoning_output_tokens=execution.reasoning_output_tokens,
            total_tokens=execution.total_tokens,
            context_compactions=execution.context_compactions,
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
    def from_record(cls, assignment: Assignment) -> AssignmentResponse:
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
    def from_record(cls, task: Task) -> TaskResponse:
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
    def from_record(cls, event: ProductEvent) -> TaskEventResponse:
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
