"""add organization-lead needs-revision task status

Revision ID: 20260724_0004
Revises: 20260724_0003
Create Date: 2026-07-24 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260724_0004"
down_revision: str | Sequence[str] | None = "20260724_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_valid_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_tasks_valid_status"),
            "status IN ('created', 'planning', 'running', 'waiting', "
            "'needs_revision', 'completed', 'failed', 'cancelled')",
        )


def downgrade() -> None:
    op.execute("UPDATE tasks SET status = 'failed' WHERE status = 'needs_revision'")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(op.f("ck_tasks_valid_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_tasks_valid_status"),
            "status IN ('created', 'planning', 'running', 'waiting', "
            "'completed', 'failed', 'cancelled')",
        )
