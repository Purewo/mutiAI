"""Product-owned Runtime workspace records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mutiai.models.base import Base, new_id, utc_now


class WorkspaceStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('provisioning', 'ready', 'failed', 'archived')",
            name="valid_status",
        ),
        UniqueConstraint(
            "organization_id",
            "agent_role_key",
            "runtime_provider",
            name="uq_workspaces_organization_role_provider",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), index=True
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.organization_id", ondelete="RESTRICT"),
        index=True,
    )
    agent_role_key: Mapped[str] = mapped_column(String(64))
    runtime_provider: Mapped[str] = mapped_column(String(32), default="codex")
    canonical_path: Mapped[str] = mapped_column(String(1_024), unique=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    codex_thread_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now, onupdate=utc_now
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    organization = relationship("Organization", back_populates="workspaces")
