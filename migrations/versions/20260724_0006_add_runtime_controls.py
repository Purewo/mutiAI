"""add Runtime concurrency, Provider capacity, and token accounting

Revision ID: 20260724_0006
Revises: 20260724_0005
Create Date: 2026-07-24 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260724_0006"
down_revision: str | Sequence[str] | None = "20260724_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_control_policies",
        sa.Column("runtime_control_policy_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("max_concurrent_executions", sa.Integer(), nullable=False),
        sa.Column("token_budget_limit", sa.BigInteger(), nullable=True),
        sa.Column("token_reservation_per_execution", sa.BigInteger(), nullable=True),
        sa.Column(
            "tokens_reserved",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "tokens_consumed",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "max_concurrent_executions > 0",
            name=op.f("ck_runtime_control_policies_positive_max_concurrent_executions"),
        ),
        sa.CheckConstraint(
            "token_budget_limit IS NULL OR token_budget_limit > 0",
            name=op.f("ck_runtime_control_policies_positive_token_budget_limit"),
        ),
        sa.CheckConstraint(
            "token_reservation_per_execution IS NULL OR "
            "token_reservation_per_execution > 0",
            name=op.f(
                "ck_runtime_control_policies_positive_token_reservation_per_execution"
            ),
        ),
        sa.CheckConstraint(
            "tokens_reserved >= 0 AND tokens_consumed >= 0",
            name=op.f("ck_runtime_control_policies_nonnegative_token_totals"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f("fk_runtime_control_policies_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "runtime_control_policy_id",
            name=op.f("pk_runtime_control_policies"),
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "provider",
            name="uq_runtime_control_policy_owner_provider",
        ),
    )
    op.create_index(
        op.f("ix_runtime_control_policies_owner_user_id"),
        "runtime_control_policies",
        ["owner_user_id"],
        unique=False,
    )

    op.create_table(
        "runtime_provider_capacities",
        sa.Column(
            "runtime_provider_capacity_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.Column("resets_at", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('available', 'limited', 'unknown')",
            name=op.f("ck_runtime_provider_capacities_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f("fk_runtime_provider_capacities_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "runtime_provider_capacity_id",
            name=op.f("pk_runtime_provider_capacities"),
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "provider",
            name="uq_runtime_provider_capacity_owner_provider",
        ),
    )
    op.create_index(
        op.f("ix_runtime_provider_capacities_owner_user_id"),
        "runtime_provider_capacities",
        ["owner_user_id"],
        unique=False,
    )

    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.add_column(
            sa.Column("wait_reason", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "reserved_tokens",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("charged_tokens", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "usage_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(sa.Column("input_tokens", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column("cached_input_tokens", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(sa.Column("output_tokens", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column("reasoning_output_tokens", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(sa.Column("total_tokens", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("admitted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("budget_settled_at", sa.DateTime(), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_runtime_executions_valid_usage_status"),
            "usage_status IN ('pending', 'reported', 'unavailable')",
        )
        batch_op.create_check_constraint(
            op.f("ck_runtime_executions_nonnegative_reserved_tokens"),
            "reserved_tokens >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_runtime_executions_nonnegative_reserved_tokens"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_runtime_executions_valid_usage_status"),
            type_="check",
        )
        for column in (
            "budget_settled_at",
            "admitted_at",
            "total_tokens",
            "reasoning_output_tokens",
            "output_tokens",
            "cached_input_tokens",
            "input_tokens",
            "usage_status",
            "charged_tokens",
            "reserved_tokens",
            "wait_reason",
        ):
            batch_op.drop_column(column)
    op.drop_index(
        op.f("ix_runtime_provider_capacities_owner_user_id"),
        table_name="runtime_provider_capacities",
    )
    op.drop_table("runtime_provider_capacities")
    op.drop_index(
        op.f("ix_runtime_control_policies_owner_user_id"),
        table_name="runtime_control_policies",
    )
    op.drop_table("runtime_control_policies")
