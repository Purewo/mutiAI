"""Persistent task, assignment, Runtime execution, and event records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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
    from mutiai.models.task_plan import Artifact, PlanStep, TaskExecutionPlan


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING = "waiting"
    NEEDS_REVISION = "needs_revision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskOrchestrationMode(StrEnum):
    """Select the compatibility workflow or the planned linear workflow."""

    LEGACY = "legacy"
    PLANNED = "planned"


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssignmentKind(StrEnum):
    LEGACY = "legacy"
    LEGACY_SPECIALIST = "legacy_specialist"
    LEGACY_LEAD_REVIEW = "legacy_lead_review"
    LEAD_PLAN = "lead_plan"
    PLAN_STEP = "plan_step"


class RuntimeExecutionStatus(StrEnum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'planning', 'running', 'waiting', "
            "'needs_revision', 'completed', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint(
            "orchestration_mode IN ('legacy', 'planned')",
            name="valid_orchestration_mode",
        ),
        UniqueConstraint(
            "owner_user_id",
            "organization_id",
            "idempotency_key",
            name="uq_tasks_owner_organization_idempotency_key",
        ),
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), index=True
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.organization_id", ondelete="RESTRICT"),
        index=True,
    )
    organization_spec_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "organization_spec_versions.spec_version_id",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    request_text: Mapped[str] = mapped_column(Text)
    capability_requirements: Mapped[dict] = mapped_column(JSON, default=dict)
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    orchestration_mode: Mapped[str] = mapped_column(
        String(16), default=TaskOrchestrationMode.LEGACY
    )
    status: Mapped[str] = mapped_column(String(20), index=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Assignment.created_at",
    )
    events: Mapped[list[ProductEvent]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ProductEvent.sequence",
    )
    execution_plans: Mapped[list[TaskExecutionPlan]] = relationship(
        "TaskExecutionPlan",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskExecutionPlan.plan_version",
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        "Artifact",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Artifact.created_at",
    )


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'submitted', 'running', 'waiting', "
            "'completed', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint(
            "assignment_kind IN ('legacy', 'legacy_specialist', "
            "'legacy_lead_review', 'lead_plan', 'plan_step')",
            name="valid_assignment_kind",
        ),
        UniqueConstraint(
            "task_id",
            "assignment_key",
            name="uq_assignments_task_key",
        ),
        UniqueConstraint("execution_id", name="uq_assignments_execution_id"),
    )

    assignment_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True
    )
    assignment_key: Mapped[str] = mapped_column(String(128))
    assignment_kind: Mapped[str] = mapped_column(String(32), index=True)
    agent_role_key: Mapped[str] = mapped_column(String(64))
    instructions: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text)
    execution_id: Mapped[str] = mapped_column(String(36))
    plan_step_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_steps.plan_step_id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), index=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    task: Mapped[Task] = relationship(back_populates="assignments")
    runtime_execution: Mapped[RuntimeExecution | None] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        uselist=False,
    )
    plan_step: Mapped[PlanStep | None] = relationship(
        "PlanStep",
        back_populates="assignment",
    )


class RuntimeExecution(Base):
    __tablename__ = "runtime_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted', 'running', 'waiting', 'completed', "
            "'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint(
            "usage_status IN ('pending', 'reported', 'unavailable')",
            name="valid_usage_status",
        ),
        CheckConstraint(
            "reserved_tokens >= 0",
            name="nonnegative_reserved_tokens",
        ),
        CheckConstraint(
            "context_compactions >= 0",
            name="nonnegative_context_compactions",
        ),
        CheckConstraint(
            "security_mode IS NULL OR security_mode IN "
            "('demo_full_access', 'workspace_restricted')",
            name="valid_security_mode",
        ),
    )

    runtime_execution_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    execution_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.assignment_id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32))
    runtime_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("runtime_bindings.runtime_binding_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    runtime_binding_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    requested_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    actual_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    security_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approval_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sandbox_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    network_access: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    runtime_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    runtime_event_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, unique=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.workspace_id", ondelete="RESTRICT"),
        nullable=True,
    )
    last_event_position: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    wait_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    charged_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_status: Mapped[str] = mapped_column(String(20), default="pending")
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasoning_output_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    context_compactions: Mapped[int] = mapped_column(Integer, default=0)
    admitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False),
        nullable=True,
    )
    budget_settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    assignment: Mapped[Assignment] = relationship(back_populates="runtime_execution")


class ProductEvent(Base):
    __tablename__ = "product_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "sequence",
            name="uq_product_events_task_sequence",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[str] = mapped_column(String(100), index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True
    )
    assignment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    runtime_execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    source: Mapped[str] = mapped_column(String(50))
    correlation_id: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON)

    task: Mapped[Task] = relationship(back_populates="events")
