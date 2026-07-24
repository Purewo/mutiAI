"""add role Runtime bindings and Thread lifecycle metadata

Revision ID: 20260725_0007
Revises: 20260724_0006
Create Date: 2026-07-25 00:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260725_0007"
down_revision: str | Sequence[str] | None = "20260724_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_bindings",
        sa.Column("runtime_binding_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("binding_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("security_mode", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "security_mode IN ('demo_full_access', 'workspace_restricted')",
            name=op.f("ck_runtime_bindings_valid_security_mode"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f("fk_runtime_bindings_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "runtime_binding_id",
            name=op.f("pk_runtime_bindings"),
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "binding_key",
            name="uq_runtime_bindings_owner_key",
        ),
    )
    op.create_index(
        op.f("ix_runtime_bindings_owner_user_id"),
        "runtime_bindings",
        ["owner_user_id"],
        unique=False,
    )

    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.add_column(
            sa.Column("runtime_binding_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("runtime_binding_key", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("requested_model", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("actual_model", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("reasoning_effort", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("security_mode", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("approval_policy", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sandbox_mode", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(sa.Column("network_access", sa.Boolean(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "context_compactions",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_foreign_key(
            op.f("fk_runtime_executions_runtime_binding_id_runtime_bindings"),
            "runtime_bindings",
            ["runtime_binding_id"],
            ["runtime_binding_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            op.f("ck_runtime_executions_nonnegative_context_compactions"),
            "context_compactions >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_runtime_executions_valid_security_mode"),
            "security_mode IS NULL OR security_mode IN "
            "('demo_full_access', 'workspace_restricted')",
        )
    op.create_index(
        op.f("ix_runtime_executions_runtime_binding_id"),
        "runtime_executions",
        ["runtime_binding_id"],
        unique=False,
    )

    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.add_column(
            sa.Column(
                "thread_compaction_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "thread_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("last_compacted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_delivery_summary", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_workspaces_nonnegative_thread_lifecycle_counts"),
            "thread_compaction_count >= 0 AND thread_generation >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_workspaces_nonnegative_thread_lifecycle_counts"),
            type_="check",
        )
        for column in (
            "last_delivery_summary",
            "last_compacted_at",
            "thread_generation",
            "thread_compaction_count",
        ):
            batch_op.drop_column(column)

    op.drop_index(
        op.f("ix_runtime_executions_runtime_binding_id"),
        table_name="runtime_executions",
    )
    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_runtime_executions_valid_security_mode"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_runtime_executions_nonnegative_context_compactions"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("fk_runtime_executions_runtime_binding_id_runtime_bindings"),
            type_="foreignkey",
        )
        for column in (
            "context_compactions",
            "network_access",
            "sandbox_mode",
            "approval_policy",
            "security_mode",
            "reasoning_effort",
            "requested_model",
            "actual_model",
            "runtime_binding_key",
            "runtime_binding_id",
        ):
            batch_op.drop_column(column)

    op.drop_index(
        op.f("ix_runtime_bindings_owner_user_id"),
        table_name="runtime_bindings",
    )
    op.drop_table("runtime_bindings")
