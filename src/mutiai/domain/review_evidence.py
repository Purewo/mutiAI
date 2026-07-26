"""Product-owned evidence supplied to an organization-lead review."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewRuntimeEvidence(BaseModel):
    """Product-observed Runtime facts for one completed Assignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_execution_id: str
    execution_id: str
    provider: str
    status: Literal["completed"]
    requested_model: str | None = None
    actual_model: str | None = None
    reasoning_effort: str | None = None
    security_mode: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    run_duration_seconds: float | None = Field(default=None, ge=0)


class ReviewAssignmentEvidence(BaseModel):
    """Durable Assignment ownership and completion facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    assignment_key: str
    assignment_kind: str
    role_key: str
    status: Literal["completed"]
    runtime: ReviewRuntimeEvidence


class ReviewArtifactEvidence(BaseModel):
    """Validated Artifact metadata without host paths or file contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    contract_key: str
    origin: Literal["task_input", "assignment"]
    producer_assignment_id: str | None = None
    producer_plan_step_id: str | None = None
    schema_version: str
    media_type: str
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    byte_size: int = Field(ge=0)
    status: Literal["released"]
    validation_summary: str


class ReviewInputBindingEvidence(BaseModel):
    """Exact immutable Artifact version materialized for one plan step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_binding_id: str
    status: Literal["materialized"]
    artifact_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    artifact: ReviewArtifactEvidence


class ReviewStepEvidence(BaseModel):
    """Product-owned evidence for one completed specialist plan step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_step_id: str
    step_key: str
    role_key: str
    sequence: int = Field(ge=0)
    status: Literal["completed"]
    depends_on_step_keys: tuple[str, ...]
    declared_input_contracts: tuple[str, ...]
    materialized_inputs: tuple[ReviewInputBindingEvidence, ...]
    declared_output_contracts: tuple[str, ...]
    released_outputs: tuple[ReviewArtifactEvidence, ...]
    assignment: ReviewAssignmentEvidence


class ReviewPlanEvidence(BaseModel):
    """Frozen execution-plan identity and validation facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    plan_version: int = Field(ge=1)
    definition_hash: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    source: str
    status: Literal["active"]
    validation_summary: str


class ReviewStepTargetEvidence(BaseModel):
    """The bounded review step and the final Artifacts it may inspect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_step_id: str
    step_key: str
    role_key: str
    depends_on_step_keys: tuple[str, ...]
    declared_input_contracts: tuple[str, ...]
    materialized_inputs: tuple[ReviewInputBindingEvidence, ...]


class ReviewEvidenceChecks(BaseModel):
    """Deterministic checks completed before the review Runtime starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    planning_assignment_completed: Literal[True]
    predecessor_steps_completed: Literal[True]
    dependency_order_satisfied: Literal[True]
    input_bindings_match_declared_contracts: Literal[True]
    outputs_match_declared_contracts: Literal[True]
    artifacts_released_and_validated: Literal[True]


class LeadReviewExecutionEvidence(BaseModel):
    """Portable product evidence for a bounded lead-review decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    source: Literal["product_database"] = "product_database"
    task_id: str
    plan: ReviewPlanEvidence
    planning_assignment: ReviewAssignmentEvidence
    specialist_steps: tuple[ReviewStepEvidence, ...]
    review_step: ReviewStepTargetEvidence
    checks: ReviewEvidenceChecks
    attestation_scope: str
