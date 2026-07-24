"""Product-owned Runtime concurrency, budget, and Provider capacity facts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mutiai.models.base import Base, new_id, utc_now


class RuntimeControlPolicy(Base):
    __tablename__ = "runtime_control_policies"
    __table_args__ = (
        CheckConstraint(
            "max_concurrent_executions > 0",
            name="positive_max_concurrent_executions",
        ),
        CheckConstraint(
            "token_budget_limit IS NULL OR token_budget_limit > 0",
            name="positive_token_budget_limit",
        ),
        CheckConstraint(
            "token_reservation_per_execution IS NULL OR "
            "token_reservation_per_execution > 0",
            name="positive_token_reservation_per_execution",
        ),
        CheckConstraint(
            "tokens_reserved >= 0 AND tokens_consumed >= 0",
            name="nonnegative_token_totals",
        ),
        UniqueConstraint(
            "owner_user_id",
            "provider",
            name="uq_runtime_control_policy_owner_provider",
        ),
    )

    runtime_control_policy_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_id,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32))
    max_concurrent_executions: Mapped[int] = mapped_column(Integer)
    token_budget_limit: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    token_reservation_per_execution: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    tokens_reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_consumed: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        default=utc_now,
        onupdate=utc_now,
    )


class RuntimeProviderCapacityRecord(Base):
    __tablename__ = "runtime_provider_capacities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('available', 'limited', 'unknown')",
            name="valid_status",
        ),
        UniqueConstraint(
            "owner_user_id",
            "provider",
            name="uq_runtime_provider_capacity_owner_provider",
        ),
    )

    runtime_provider_capacity_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=new_id,
    )
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False),
        nullable=True,
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))

