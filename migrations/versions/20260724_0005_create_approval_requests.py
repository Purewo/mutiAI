"""create product-owned Runtime approval requests

Revision ID: 20260724_0005
Revises: 20260724_0004
Create Date: 2026-07-24 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260724_0005"
down_revision: str | Sequence[str] | None = "20260724_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("approval_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("assignment_id", sa.String(length=36), nullable=False),
        sa.Column("runtime_execution_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("runtime_request_id", sa.String(length=256), nullable=False),
        sa.Column("thread_id", sa.String(length=100), nullable=False),
        sa.Column("turn_id", sa.String(length=100), nullable=False),
        sa.Column("item_id", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("runtime_started_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=True),
        sa.Column("decided_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('command_execution', 'file_change')",
            name=op.f("ck_approval_requests_valid_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'cancelled')",
            name=op.f("ck_approval_requests_valid_status"),
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('accept', 'decline', 'cancel')",
            name=op.f("ck_approval_requests_valid_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["assignments.assignment_id"],
            name=op.f("fk_approval_requests_assignment_id_assignments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.user_id"],
            name=op.f("fk_approval_requests_decided_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_execution_id"],
            ["runtime_executions.runtime_execution_id"],
            name=op.f("fk_approval_requests_runtime_execution_id_runtime_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name=op.f("fk_approval_requests_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("approval_id", name=op.f("pk_approval_requests")),
        sa.UniqueConstraint(
            "runtime_execution_id",
            "turn_id",
            "runtime_request_id",
            name="uq_approval_runtime_turn_request",
        ),
    )
    op.create_index(
        op.f("ix_approval_requests_task_id"),
        "approval_requests",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_approval_requests_assignment_id"),
        "approval_requests",
        ["assignment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_approval_requests_runtime_execution_id"),
        "approval_requests",
        ["runtime_execution_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_approval_requests_kind"),
        "approval_requests",
        ["kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_approval_requests_status"),
        "approval_requests",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_approval_requests_status"), table_name="approval_requests")
    op.drop_index(op.f("ix_approval_requests_kind"), table_name="approval_requests")
    op.drop_index(
        op.f("ix_approval_requests_runtime_execution_id"),
        table_name="approval_requests",
    )
    op.drop_index(
        op.f("ix_approval_requests_assignment_id"),
        table_name="approval_requests",
    )
    op.drop_index(op.f("ix_approval_requests_task_id"), table_name="approval_requests")
    op.drop_table("approval_requests")
