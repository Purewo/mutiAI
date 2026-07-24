"""Product-owned Runtime binding definitions for formal organization roles."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mutiai.models.base import Base, new_id, utc_now


class RuntimeSecurityMode(StrEnum):
    """Named product security profiles compiled into Runtime-specific policy."""

    DEMO_FULL_ACCESS = "demo_full_access"
    WORKSPACE_RESTRICTED = "workspace_restricted"


class RuntimeBinding(Base):
    """Version-independent Runtime configuration referenced by a role key."""

    __tablename__ = "runtime_bindings"
    __table_args__ = (
        CheckConstraint(
            "security_mode IN ('demo_full_access', 'workspace_restricted')",
            name="valid_security_mode",
        ),
        UniqueConstraint(
            "owner_user_id",
            "binding_key",
            name="uq_runtime_bindings_owner_key",
        ),
    )

    runtime_binding_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_id,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"),
        index=True,
    )
    binding_key: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    security_mode: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        default=utc_now,
        onupdate=utc_now,
    )
