"""Persisted Task execution plans and cross-role Artifact records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mutiai.models.base import Base, new_id, utc_now

if TYPE_CHECKING:
    from mutiai.models.task import Assignment, Task


class TaskExecutionPlanStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    ACTIVE = "active"
    COMPLETED = "completed"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStepStatus(StrEnum):
    PENDING_DEPENDENCY = "pending_dependency"
    READY = "ready"
    SUBMITTED = "submitted"
    RUNNING = "running"
    WAITING = "waiting"
    VALIDATING_OUTPUT = "validating_output"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    RELEASED = "released"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ArtifactInputBindingStatus(StrEnum):
    MATERIALIZED = "materialized"
    REVOKED = "revoked"


class TaskExecutionPlan(Base):
    __tablename__ = "task_execution_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'validated', 'active', 'completed', "
            "'needs_revision', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("plan_version >= 1", name="positive_plan_version"),
        UniqueConstraint(
            "task_id",
            "plan_version",
            name="uq_task_execution_plans_task_version",
        ),
    )

    plan_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True
    )
    organization_spec_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "organization_spec_versions.spec_version_id",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    definition_hash: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    validation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    initial_input_contracts: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    task: Mapped[Task] = relationship("Task", back_populates="execution_plans")
    steps: Mapped[list[PlanStep]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="PlanStep.sequence",
    )


class PlanStep(Base):
    __tablename__ = "plan_steps"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_dependency', 'ready', 'submitted', 'running', "
            "'waiting', 'validating_output', 'completed', 'blocked', 'failed', "
            "'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("sequence >= 0", name="nonnegative_sequence"),
        UniqueConstraint(
            "plan_id",
            "step_key",
            name="uq_plan_steps_plan_key",
        ),
        UniqueConstraint(
            "plan_id",
            "sequence",
            name="uq_plan_steps_plan_sequence",
        ),
    )

    plan_step_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("task_execution_plans.plan_id", ondelete="CASCADE"),
        index=True,
    )
    step_key: Mapped[str] = mapped_column(String(64))
    role_key: Mapped[str] = mapped_column(String(64), index=True)
    step_kind: Mapped[str] = mapped_column(String(32), default="specialist")
    sequence: Mapped[int] = mapped_column(Integer)
    objective: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text)
    input_contracts: Mapped[list[str]] = mapped_column(JSON, default=list)
    output_contracts: Mapped[list[dict]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    plan: Mapped[TaskExecutionPlan] = relationship(back_populates="steps")
    dependencies: Mapped[list[PlanStepDependency]] = relationship(
        foreign_keys="PlanStepDependency.plan_step_id",
        cascade="all, delete-orphan",
        order_by="PlanStepDependency.created_at",
    )
    assignment: Mapped[Assignment | None] = relationship(
        "Assignment",
        back_populates="plan_step",
        uselist=False,
    )
    input_bindings: Mapped[list[ArtifactInputBinding]] = relationship(
        back_populates="plan_step",
        cascade="all, delete-orphan",
        order_by="ArtifactInputBinding.created_at",
    )


class PlanStepDependency(Base):
    __tablename__ = "plan_step_dependencies"
    __table_args__ = (
        CheckConstraint(
            "plan_step_id <> depends_on_step_id",
            name="different_steps",
        ),
        UniqueConstraint(
            "plan_step_id",
            "depends_on_step_id",
            name="uq_plan_step_dependencies_edge",
        ),
    )

    dependency_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    plan_step_id: Mapped[str] = mapped_column(
        ForeignKey("plan_steps.plan_step_id", ondelete="CASCADE"),
        index=True,
    )
    depends_on_step_id: Mapped[str] = mapped_column(
        ForeignKey("plan_steps.plan_step_id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'validated', 'released', 'rejected', "
            "'superseded')",
            name="valid_status",
        ),
        CheckConstraint(
            "origin IN ('task_input', 'assignment')",
            name="valid_origin",
        ),
        CheckConstraint(
            "(origin = 'task_input' AND producer_assignment_id IS NULL AND "
            "producer_plan_step_id IS NULL AND source_workspace_id IS NULL) OR "
            "(origin = 'assignment' AND producer_assignment_id IS NOT NULL AND "
            "producer_plan_step_id IS NOT NULL AND source_workspace_id IS NOT NULL)",
            name="valid_origin_ownership",
        ),
        CheckConstraint("byte_size >= 0", name="nonnegative_byte_size"),
        CheckConstraint("artifact_version >= 1", name="positive_artifact_version"),
        UniqueConstraint(
            "task_id",
            "contract_key",
            "artifact_version",
            name="uq_artifacts_task_contract_version",
        ),
        UniqueConstraint(
            "task_id",
            "contract_key",
            "source_delivery_id",
            name="uq_artifacts_task_contract_delivery",
        ),
        UniqueConstraint(
            "task_id",
            "storage_relative_path",
            name="uq_artifacts_task_storage_path",
        ),
    )

    artifact_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[str] = mapped_column(String(20), index=True)
    source_delivery_id: Mapped[str] = mapped_column(String(100), index=True)
    producer_assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("assignments.assignment_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    producer_plan_step_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_steps.plan_step_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    source_workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.workspace_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    contract_key: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    artifact_version: Mapped[int] = mapped_column(Integer, default=1)
    media_type: Mapped[str] = mapped_column(String(255))
    file_name: Mapped[str] = mapped_column(String(255))
    source_relative_path: Mapped[str] = mapped_column(String(1_024))
    storage_relative_path: Mapped[str] = mapped_column(String(1_024))
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), index=True)
    validation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    task: Mapped[Task] = relationship("Task", back_populates="artifacts")
    input_bindings: Mapped[list[ArtifactInputBinding]] = relationship(
        back_populates="artifact",
        order_by="ArtifactInputBinding.created_at",
    )


class ArtifactInputBinding(Base):
    __tablename__ = "artifact_input_bindings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('materialized', 'revoked')",
            name="valid_status",
        ),
        UniqueConstraint(
            "plan_step_id",
            "artifact_id",
            name="uq_artifact_input_bindings_step_artifact",
        ),
    )

    input_binding_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True
    )
    plan_step_id: Mapped[str] = mapped_column(
        ForeignKey("plan_steps.plan_step_id", ondelete="CASCADE"), index=True
    )
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"), index=True
    )
    consumer_workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.workspace_id", ondelete="RESTRICT"), index=True
    )
    materialized_relative_path: Mapped[str] = mapped_column(String(1_024))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    plan_step: Mapped[PlanStep] = relationship(back_populates="input_bindings")
    artifact: Mapped[Artifact] = relationship(back_populates="input_bindings")
