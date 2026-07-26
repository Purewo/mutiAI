"""add assistant action error context

Revision ID: 20260727_0012
Revises: 07468d4d9da8
Create Date: 2026-07-27 04:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0012"
down_revision: str | Sequence[str] | None = "07468d4d9da8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_actions",
        sa.Column("error_status_code", sa.Integer(), nullable=True),
    )
    op.add_column(
        "assistant_actions",
        sa.Column("error_details", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_actions", "error_details")
    op.drop_column("assistant_actions", "error_status_code")
