"""add runtime completion event identity

Revision ID: c89ba494d06f
Revises: 39bba66c6ae1
Create Date: 2026-07-24 01:05:37.355181
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c89ba494d06f"
down_revision: Union[str, Sequence[str], None] = "39bba66c6ae1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.add_column(
            sa.Column("runtime_event_id", sa.String(length=100), nullable=True)
        )
        batch_op.create_unique_constraint(
            op.f("uq_runtime_executions_runtime_event_id"),
            ["runtime_event_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("runtime_executions") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_runtime_executions_runtime_event_id"),
            type_="unique",
        )
        batch_op.drop_column("runtime_event_id")
