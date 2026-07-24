"""Persistent task, assignment, Runtime execution, and event records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

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


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING = "waiting"
    NEEDS_REVISION = "needs_revision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
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


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'submitted', 'running', 'waiting', "
            "'completed', 'failed', 'cancelled')",
            name="valid_status",
        ),
        UniqueConstraint(
            "task_id",
            "agent_role_key",
            name="uq_assignments_task_role_key",
        ),
        UniqueConstraint("execution_id", name="uq_assignments_execution_id"),
    )

    assignment_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True
    )
    agent_role_key: Mapped[str] = mapped_column(String(64))
    instructions: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text)
    execution_id: Mapped[str] = mapped_column(String(36))
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
