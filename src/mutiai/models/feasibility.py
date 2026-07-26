"""Product-owned Runtime capability revisions and feasibility evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mutiai.models.base import Base, new_id, utc_now


class FeasibilityOutcome(StrEnum):
    FEASIBLE = "feasible"
    CONDITIONAL = "conditional"
    BLOCKED = "blocked"
    CAPABILITY_UNKNOWN = "capability_unknown"


class RuntimeCapabilityProfile(Base):
    """One immutable capability revision for a Runtime binding."""

    __tablename__ = "runtime_capability_profiles"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="positive_revision"),
        UniqueConstraint(
            "runtime_binding_id",
            "revision",
            name="uq_runtime_capability_profiles_binding_revision",
        ),
    )

    capability_profile_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_id,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"),
        index=True,
    )
    runtime_binding_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_bindings.runtime_binding_id", ondelete="CASCADE"),
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    profile_payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(50))
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )


class FeasibilityCheck(Base):
    """Immutable evidence for one organization, Task, or Runtime gate."""

    __tablename__ = "feasibility_checks"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('feasible', 'conditional', 'blocked', "
            "'capability_unknown')",
            name="valid_outcome",
        ),
    )

    feasibility_check_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_id,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"),
        index=True,
    )
    target_type: Mapped[str] = mapped_column(String(50), index=True)
    target_id: Mapped[str] = mapped_column(String(100), index=True)
    phase: Mapped[str] = mapped_column(String(50), index=True)
    validator_version: Mapped[str] = mapped_column(String(20))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    requirements_payload: Mapped[list] = mapped_column(JSON)
    profile_revisions_payload: Mapped[list] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    findings_payload: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
