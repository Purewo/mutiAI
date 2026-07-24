"""Product-owned Runtime approval requests and user decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mutiai.models.base import Base, new_id, utc_now


class ApprovalKind(StrEnum):
    COMMAND_EXECUTION = "command_execution"
    FILE_CHANGE = "file_change"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('command_execution', 'file_change')",
            name="valid_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('accept', 'decline', 'cancel')",
            name="valid_decision",
        ),
        UniqueConstraint(
            "runtime_execution_id",
            "turn_id",
            "runtime_request_id",
            name="uq_approval_runtime_turn_request",
        ),
    )

    approval_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"), index=True
    )
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.assignment_id", ondelete="CASCADE"), index=True
    )
    runtime_execution_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_executions.runtime_execution_id", ondelete="CASCADE"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    runtime_request_id: Mapped[str] = mapped_column(String(256))
    thread_id: Mapped[str] = mapped_column(String(100))
    turn_id: Mapped[str] = mapped_column(String(100))
    item_id: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    runtime_started_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
