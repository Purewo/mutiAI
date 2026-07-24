"""add Task orchestration mode

Revision ID: 20260725_0010
Revises: 20260725_0009
Create Date: 2026-07-25 05:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260725_0010"
down_revision: str | Sequence[str] | None = "20260725_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "orchestration_mode",
                sa.String(length=16),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_tasks_valid_orchestration_mode"),
            "orchestration_mode IN ('legacy', 'planned')",
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_tasks_valid_orchestration_mode"),
            type_="check",
        )
        batch_op.drop_column("orchestration_mode")
