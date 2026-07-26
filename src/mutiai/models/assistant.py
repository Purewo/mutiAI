"""Product-owned platform-assistant conversation records.

These records intentionally contain only user-visible messages, product actions,
stable Runtime identities, and resumable event cursors. Codex's private
transcript and tool activity remain in its own Thread store.
"""

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


class AssistantConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AssistantMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    EVENT = "event"


class AssistantMessageStatus(StrEnum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"


class AssistantTurnStatus(StrEnum):
    QUEUED = "queued"
    SUBMITTED = "submitted"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssistantActionStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="valid_status"),
        CheckConstraint(
            "runtime_thread_generation >= 0 AND observed_compactions >= 0",
            name="nonnegative_thread_counters",
        ),
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=AssistantConversationStatus.ACTIVE
    )
    runtime_provider: Mapped[str] = mapped_column(String(32), default="codex")
    runtime_thread_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, unique=True
    )
    runtime_workspace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    runtime_workspace_path: Mapped[str | None] = mapped_column(
        String(1_024), nullable=True
    )
    runtime_thread_generation: Mapped[int] = mapped_column(Integer, default=0)
    observed_compactions: Mapped[int] = mapped_column(Integer, default=0)
    tool_contract_version: Mapped[str] = mapped_column(String(20), default="1.0")
    system_prompt_version: Mapped[str] = mapped_column(String(128), default="1.0")
    rolling_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now, onupdate=utc_now
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    owner = relationship("User", back_populates="assistant_conversations")
    messages: Mapped[list[AssistantMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.sequence",
    )
    turns: Mapped[list[AssistantTurn]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantTurn.created_at",
    )
    actions: Mapped[list[AssistantAction]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantAction.proposed_at",
    )
    events: Mapped[list[AssistantEvent]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantEvent.sequence",
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_assistant_messages_sequence"
        ),
        UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_assistant_messages_idempotency",
        ),
        CheckConstraint("role IN ('user', 'assistant', 'event')", name="valid_role"),
        CheckConstraint(
            "status IN ('accepted', 'completed', 'failed')",
            name="valid_status",
        ),
    )

    message_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(
        String(20), default=AssistantMessageStatus.ACCEPTED
    )
    text_content: Mapped[str] = mapped_column(Text, default="")
    content_blocks: Mapped[list] = mapped_column(JSON, default=list)
    attachment_refs: Mapped[list] = mapped_column(JSON, default=list)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reply_to_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    related_resource_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    related_resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    conversation: Mapped[AssistantConversation] = relationship(
        back_populates="messages"
    )


class AssistantTurn(Base):
    __tablename__ = "assistant_turns"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_assistant_turns_idempotency",
        ),
        CheckConstraint(
            "status IN ('queued', 'submitted', 'running', 'waiting', "
            "'completed', 'failed', 'cancelled')",
            name="valid_status",
        ),
    )

    turn_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_messages.message_id", ondelete="RESTRICT"),
        unique=True,
    )
    execution_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    runtime_provider: Mapped[str] = mapped_column(String(32))
    runtime_thread_generation: Mapped[int] = mapped_column(Integer, default=0)
    runtime_thread_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    runtime_turn_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    runtime_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=AssistantTurnStatus.QUEUED)
    requested_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    actual_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    final_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasoning_output_tokens: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    context_compactions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    conversation: Mapped[AssistantConversation] = relationship(back_populates="turns")
    source_message: Mapped[AssistantMessage] = relationship(
        foreign_keys=[source_message_id]
    )


class AssistantAction(Base):
    __tablename__ = "assistant_actions"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_assistant_actions_idempotency",
        ),
        CheckConstraint(
            "status IN ('proposed', 'confirmed', 'executing', 'completed', "
            "'failed', 'declined', 'cancelled', 'expired', 'superseded')",
            name="valid_status",
        ),
    )

    action_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    source_turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_turns.turn_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(20), default=AssistantActionStatus.PROPOSED
    )
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_details: Mapped[dict | list | str | int | float | bool | None] = (
        mapped_column(JSON, nullable=True)
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    conversation: Mapped[AssistantConversation] = relationship(back_populates="actions")
    source_turn: Mapped[AssistantTurn | None] = relationship()


class AssistantEvent(Base):
    __tablename__ = "assistant_events"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_assistant_events_sequence"
        ),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[str] = mapped_column(String(100), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utc_now
    )
    source: Mapped[str] = mapped_column(String(50))
    correlation_id: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    conversation: Mapped[AssistantConversation] = relationship(back_populates="events")
