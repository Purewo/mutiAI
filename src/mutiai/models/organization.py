"""Persistent organization and OrganizationSpec version records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mutiai.models.base import Base, new_id, utc_now
from mutiai.models.workspace import Workspace


class OrganizationVersionStatus(StrEnum):
    PROPOSAL = "proposal"
    CONFIRMED = "confirmed"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class Organization(Base):
    __tablename__ = "organizations"

    organization_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(2_000), default="")
    current_published_version_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now, onupdate=utc_now
    )

    owner = relationship("User", back_populates="organizations")
    workspaces: Mapped[list[Workspace]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    versions: Mapped[list[OrganizationSpecVersion]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        order_by="OrganizationSpecVersion.version_number",
    )


class OrganizationSpecVersion(Base):
    __tablename__ = "organization_spec_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposal', 'confirmed', 'published', "
            "'superseded', 'archived')",
            name="valid_status",
        ),
        UniqueConstraint(
            "organization_id",
            "version_number",
            name="uq_organization_spec_versions_organization_version",
        ),
    )

    spec_version_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        index=True,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), index=True)
    spec_payload: Mapped[dict] = mapped_column(JSON)
    source_request: Mapped[str | None] = mapped_column(String(4_000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    organization: Mapped[Organization] = relationship(back_populates="versions")
