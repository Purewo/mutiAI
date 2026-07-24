"""add durable Assignment keys and kinds

Revision ID: 20260725_0009
Revises: 20260725_0008
Create Date: 2026-07-25 04:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260725_0009"
down_revision: str | Sequence[str] | None = "20260725_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("assignments") as batch_op:
        batch_op.add_column(
            sa.Column("assignment_key", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("assignment_kind", sa.String(length=32), nullable=True)
        )

    op.execute(
        sa.text(
            "UPDATE assignments SET assignment_key = agent_role_key, "
            "assignment_kind = 'legacy'"
        )
    )

    with op.batch_alter_table("assignments") as batch_op:
        batch_op.alter_column(
            "assignment_key",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.alter_column(
            "assignment_kind",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch_op.drop_constraint(
            "uq_assignments_task_role_key",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_assignments_task_key",
            ["task_id", "assignment_key"],
        )
        batch_op.create_check_constraint(
            op.f("ck_assignments_valid_assignment_kind"),
            "assignment_kind IN ('legacy', 'legacy_specialist', "
            "'legacy_lead_review', 'lead_plan', 'plan_step')",
        )
    op.create_index(
        op.f("ix_assignments_assignment_kind"),
        "assignments",
        ["assignment_kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_assignments_assignment_kind"),
        table_name="assignments",
    )
    with op.batch_alter_table("assignments") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_assignments_valid_assignment_kind"),
            type_="check",
        )
        batch_op.drop_constraint(
            "uq_assignments_task_key",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_assignments_task_role_key",
            ["task_id", "agent_role_key"],
        )
        batch_op.drop_column("assignment_kind")
        batch_op.drop_column("assignment_key")
