"""Task, assignment, Runtime execution, and event API contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from mutiai.api.schemas.organizations import as_utc
from mutiai.domain import WorkloadRequirements
from mutiai.models import (
    Artifact,
    ArtifactInputBinding,
    Assignment,
    PlanStep,
    ProductEvent,
    RuntimeExecution,
    Task,
    TaskExecutionPlan,
)
from mutiai.models.task import (
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskOrchestrationMode,
    TaskStatus,
)
from mutiai.models.task_plan import (
    ArtifactInputBindingStatus,
    ArtifactStatus,
    PlanStepStatus,
    TaskExecutionPlanStatus,
)


def _duration_seconds(
    started_at: datetime | None,
    completed_at: datetime | None,
) -> float | None:
    """Derive a nonnegative wall-clock duration from persisted timestamps."""

    if started_at is None or completed_at is None:
        return None
    return round(max(0.0, (completed_at - started_at).total_seconds()), 6)


class TaskCreateRequest(BaseModel):
    request: str = Field(min_length=1, max_length=10_000)
    orchestration_mode: TaskOrchestrationMode = TaskOrchestrationMode.LEGACY
    capability_requirements: WorkloadRequirements = Field(
        default_factory=WorkloadRequirements
    )


class TaskInputArtifactRequest(BaseModel):
    contract_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    schema_version: str = Field(default="1.0", min_length=1, max_length=20)
    media_type: str = Field(min_length=1, max_length=255)
    file_name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=28_000_000)
    source_delivery_id: str = Field(min_length=1, max_length=100)


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
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    queue_duration_seconds: float | None
    run_duration_seconds: float | None
    wall_duration_seconds: float | None

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
            created_at=as_utc(execution.created_at),
            started_at=as_utc(execution.started_at),
            completed_at=as_utc(execution.completed_at),
            queue_duration_seconds=_duration_seconds(
                execution.created_at,
                execution.started_at,
            ),
            run_duration_seconds=_duration_seconds(
                execution.started_at,
                execution.completed_at,
            ),
            wall_duration_seconds=_duration_seconds(
                execution.created_at,
                execution.completed_at,
            ),
        )


class AssignmentResponse(BaseModel):
    assignment_id: str
    assignment_key: str
    assignment_kind: str
    agent_role_key: str
    instructions: str
    acceptance_criteria: str
    execution_id: str
    plan_step_id: str | None
    status: AssignmentStatus
    result_summary: str | None
    runtime_execution: RuntimeExecutionResponse | None
    created_at: datetime
    completed_at: datetime | None
    wall_duration_seconds: float | None

    @classmethod
    def from_record(cls, assignment: Assignment) -> AssignmentResponse:
        return cls(
            assignment_id=assignment.assignment_id,
            assignment_key=assignment.assignment_key,
            assignment_kind=assignment.assignment_kind,
            agent_role_key=assignment.agent_role_key,
            instructions=assignment.instructions,
            acceptance_criteria=assignment.acceptance_criteria,
            execution_id=assignment.execution_id,
            plan_step_id=assignment.plan_step_id,
            status=AssignmentStatus(assignment.status),
            result_summary=assignment.result_summary,
            runtime_execution=(
                RuntimeExecutionResponse.from_record(assignment.runtime_execution)
                if assignment.runtime_execution is not None
                else None
            ),
            created_at=as_utc(assignment.created_at),
            completed_at=as_utc(assignment.completed_at),
            wall_duration_seconds=_duration_seconds(
                assignment.created_at,
                assignment.completed_at,
            ),
        )


class ArtifactInputBindingResponse(BaseModel):
    input_binding_id: str
    artifact_id: str
    consumer_workspace_id: str
    materialized_relative_path: str
    artifact_sha256: str
    status: ArtifactInputBindingStatus
    created_at: datetime
    revoked_at: datetime | None

    @classmethod
    def from_record(
        cls,
        binding: ArtifactInputBinding,
    ) -> ArtifactInputBindingResponse:
        return cls(
            input_binding_id=binding.input_binding_id,
            artifact_id=binding.artifact_id,
            consumer_workspace_id=binding.consumer_workspace_id,
            materialized_relative_path=binding.materialized_relative_path,
            artifact_sha256=binding.artifact_sha256,
            status=ArtifactInputBindingStatus(binding.status),
            created_at=as_utc(binding.created_at),
            revoked_at=as_utc(binding.revoked_at),
        )


class PlanStepResponse(BaseModel):
    plan_step_id: str
    step_key: str
    role_key: str
    step_kind: str
    sequence: int
    objective: str
    acceptance_criteria: str
    input_contracts: list[str]
    output_contracts: list[dict]
    dependency_step_ids: list[str]
    status: PlanStepStatus
    assignment_id: str | None
    input_bindings: list[ArtifactInputBindingResponse]
    created_at: datetime
    ready_at: datetime | None
    completed_at: datetime | None
    dependency_wait_seconds: float | None
    active_duration_seconds: float | None

    @classmethod
    def from_record(cls, step: PlanStep) -> PlanStepResponse:
        return cls(
            plan_step_id=step.plan_step_id,
            step_key=step.step_key,
            role_key=step.role_key,
            step_kind=step.step_kind,
            sequence=step.sequence,
            objective=step.objective,
            acceptance_criteria=step.acceptance_criteria,
            input_contracts=list(step.input_contracts),
            output_contracts=list(step.output_contracts),
            dependency_step_ids=[
                dependency.depends_on_step_id for dependency in step.dependencies
            ],
            status=PlanStepStatus(step.status),
            assignment_id=(
                step.assignment.assignment_id if step.assignment is not None else None
            ),
            input_bindings=[
                ArtifactInputBindingResponse.from_record(binding)
                for binding in step.input_bindings
            ],
            created_at=as_utc(step.created_at),
            ready_at=as_utc(step.ready_at),
            completed_at=as_utc(step.completed_at),
            dependency_wait_seconds=_duration_seconds(
                step.created_at,
                step.ready_at,
            ),
            active_duration_seconds=_duration_seconds(
                step.ready_at,
                step.completed_at,
            ),
        )


class TaskExecutionPlanResponse(BaseModel):
    plan_id: str
    plan_version: int
    schema_version: str
    definition_hash: str
    source: str
    status: TaskExecutionPlanStatus
    summary: str
    validation_summary: str | None
    initial_input_contracts: list[str]
    steps: list[PlanStepResponse]
    created_at: datetime
    activated_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_record(cls, plan: TaskExecutionPlan) -> TaskExecutionPlanResponse:
        return cls(
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            schema_version=plan.schema_version,
            definition_hash=plan.definition_hash,
            source=plan.source,
            status=TaskExecutionPlanStatus(plan.status),
            summary=plan.summary,
            validation_summary=plan.validation_summary,
            initial_input_contracts=list(plan.initial_input_contracts),
            steps=[PlanStepResponse.from_record(step) for step in plan.steps],
            created_at=as_utc(plan.created_at),
            activated_at=as_utc(plan.activated_at),
            completed_at=as_utc(plan.completed_at),
        )


class ArtifactResponse(BaseModel):
    artifact_id: str
    origin: str
    source_delivery_id: str
    producer_assignment_id: str | None
    producer_plan_step_id: str | None
    source_workspace_id: str | None
    contract_key: str
    schema_version: str
    artifact_version: int
    media_type: str
    file_name: str
    storage_relative_path: str
    sha256: str
    byte_size: int
    status: ArtifactStatus
    validation_summary: str | None
    supersedes_artifact_id: str | None
    created_at: datetime
    released_at: datetime | None
    content_url: str
    download_url: str

    @classmethod
    def from_record(cls, artifact: Artifact) -> ArtifactResponse:
        content_url = (
            f"/api/v1/tasks/{artifact.task_id}/artifacts/"
            f"{artifact.artifact_id}/content"
        )
        return cls(
            artifact_id=artifact.artifact_id,
            origin=artifact.origin,
            source_delivery_id=artifact.source_delivery_id,
            producer_assignment_id=artifact.producer_assignment_id,
            producer_plan_step_id=artifact.producer_plan_step_id,
            source_workspace_id=artifact.source_workspace_id,
            contract_key=artifact.contract_key,
            schema_version=artifact.schema_version,
            artifact_version=artifact.artifact_version,
            media_type=artifact.media_type,
            file_name=artifact.file_name,
            storage_relative_path=artifact.storage_relative_path,
            sha256=artifact.sha256,
            byte_size=artifact.byte_size,
            status=ArtifactStatus(artifact.status),
            validation_summary=artifact.validation_summary,
            supersedes_artifact_id=artifact.supersedes_artifact_id,
            created_at=as_utc(artifact.created_at),
            released_at=as_utc(artifact.released_at),
            content_url=content_url,
            download_url=f"{content_url}?download=true",
        )


class AssignmentTokenUsageResponse(BaseModel):
    assignment_id: str
    assignment_key: str
    assignment_kind: str
    agent_role_key: str
    runtime_execution_id: str
    execution_id: str
    provider: str
    requested_model: str | None
    actual_model: str | None
    execution_status: RuntimeExecutionStatus
    usage_status: str
    reserved_tokens: int
    charged_tokens: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None

    @classmethod
    def from_record(
        cls,
        assignment: Assignment,
        execution: RuntimeExecution,
    ) -> AssignmentTokenUsageResponse:
        return cls(
            assignment_id=assignment.assignment_id,
            assignment_key=assignment.assignment_key,
            assignment_kind=assignment.assignment_kind,
            agent_role_key=assignment.agent_role_key,
            runtime_execution_id=execution.runtime_execution_id,
            execution_id=execution.execution_id,
            provider=execution.provider,
            requested_model=execution.requested_model,
            actual_model=execution.actual_model,
            execution_status=RuntimeExecutionStatus(execution.status),
            usage_status=execution.usage_status,
            reserved_tokens=execution.reserved_tokens,
            charged_tokens=execution.charged_tokens,
            input_tokens=execution.input_tokens,
            cached_input_tokens=execution.cached_input_tokens,
            output_tokens=execution.output_tokens,
            reasoning_output_tokens=execution.reasoning_output_tokens,
            total_tokens=execution.total_tokens,
        )


class TaskTokenUsageResponse(BaseModel):
    task_id: str
    execution_count: int
    reported_execution_count: int
    unavailable_execution_count: int
    pending_execution_count: int
    reserved_tokens: int
    charged_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    observed_total_tokens: int
    assignments: list[AssignmentTokenUsageResponse]

    @classmethod
    def from_records(
        cls,
        task_id: str,
        records: list[tuple[Assignment, RuntimeExecution]],
    ) -> TaskTokenUsageResponse:
        reported = [
            (assignment, execution)
            for assignment, execution in records
            if execution.usage_status == "reported"
        ]
        return cls(
            task_id=task_id,
            execution_count=len(records),
            reported_execution_count=len(reported),
            unavailable_execution_count=sum(
                execution.usage_status == "unavailable"
                for _, execution in records
            ),
            pending_execution_count=sum(
                execution.usage_status == "pending" for _, execution in records
            ),
            reserved_tokens=sum(execution.reserved_tokens for _, execution in records),
            charged_tokens=sum(
                execution.charged_tokens or 0 for _, execution in records
            ),
            input_tokens=sum(execution.input_tokens or 0 for _, execution in reported),
            cached_input_tokens=sum(
                execution.cached_input_tokens or 0 for _, execution in reported
            ),
            output_tokens=sum(
                execution.output_tokens or 0 for _, execution in reported
            ),
            reasoning_output_tokens=sum(
                execution.reasoning_output_tokens or 0
                for _, execution in reported
            ),
            observed_total_tokens=sum(
                execution.total_tokens or 0 for _, execution in reported
            ),
            assignments=[
                AssignmentTokenUsageResponse.from_record(assignment, execution)
                for assignment, execution in records
            ],
        )


class TaskResponse(BaseModel):
    task_id: str
    organization_id: str
    organization_spec_version_id: str
    request: str
    capability_requirements: WorkloadRequirements
    orchestration_mode: TaskOrchestrationMode
    status: TaskStatus
    result_summary: str | None
    assignments: list[AssignmentResponse]
    execution_plan: TaskExecutionPlanResponse | None
    artifacts: list[ArtifactResponse]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    wall_duration_seconds: float | None

    @classmethod
    def from_record(cls, task: Task) -> TaskResponse:
        return cls(
            task_id=task.task_id,
            organization_id=task.organization_id,
            organization_spec_version_id=task.organization_spec_version_id,
            request=task.request_text,
            capability_requirements=WorkloadRequirements.model_validate(
                task.capability_requirements or {}
            ),
            orchestration_mode=TaskOrchestrationMode(task.orchestration_mode),
            status=TaskStatus(task.status),
            result_summary=task.result_summary,
            assignments=[
                AssignmentResponse.from_record(assignment)
                for assignment in task.assignments
            ],
            execution_plan=(
                TaskExecutionPlanResponse.from_record(task.execution_plans[-1])
                if task.execution_plans
                else None
            ),
            artifacts=[
                ArtifactResponse.from_record(artifact) for artifact in task.artifacts
            ],
            created_at=as_utc(task.created_at),
            updated_at=as_utc(task.updated_at),
            completed_at=as_utc(task.completed_at),
            wall_duration_seconds=_duration_seconds(
                task.created_at,
                task.completed_at,
            ),
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
