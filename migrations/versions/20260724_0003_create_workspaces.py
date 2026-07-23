"""create product-owned Runtime workspaces

Revision ID: 20260724_0003
Revises: c89ba494d06f
Create Date: 2026-07-24 03:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260724_0003"
down_revision: Union[str, Sequence[str], None] = "c89ba494d06f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("agent_role_key", sa.String(length=64), nullable=False),
        sa.Column(
            "runtime_provider",
            sa.String(length=32),
            nullable=False,
            server_default="codex",
        ),
        sa.Column("canonical_path", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("codex_thread_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("ready_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('provisioning', 'ready', 'failed', 'archived')",
            name=op.f("ck_workspaces_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name=op.f("fk_workspaces_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f("fk_workspaces_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("canonical_path", name="uq_workspaces_canonical_path"),
        sa.UniqueConstraint(
            "codex_thread_id",
            name="uq_workspaces_codex_thread_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "agent_role_key",
            "runtime_provider",
            name="uq_workspaces_organization_role_provider",
        ),
    )
    op.create_index(
        op.f("ix_workspaces_owner_user_id"),
        "workspaces",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspaces_organization_id"),
        "workspaces",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspaces_status"),
        "workspaces",
        ["status"],
        unique=False,
    )

    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_runtime_executions_workspace_id_workspaces"),
            "workspaces",
            ["workspace_id"],
            ["workspace_id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_runtime_executions_workspace_id_workspaces"),
            type_="foreignkey",
        )
    op.drop_index(op.f("ix_workspaces_status"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_organization_id"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_owner_user_id"), table_name="workspaces")
    op.drop_table("workspaces")
