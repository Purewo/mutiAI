"""Persistent user and browser-session records."""

from __future__ import annotations

from datetime import datetime, UTC
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mutiai.models.base import Base


def utc_now() -> datetime:
    """Return a naive datetime whose value is UTC for SQLite portability."""

    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid4())


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )

    sessions: Mapped[list[BrowserSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class BrowserSession(Base):
    __tablename__ = "browser_sessions"

    session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="sessions")
