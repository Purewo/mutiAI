"""add version identity to assistant message content

Revision ID: 20260727_0013
Revises: 20260727_0012
Create Date: 2026-07-27 06:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0013"
down_revision: str | Sequence[str] | None = "20260727_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_messages",
        sa.Column(
            "content_schema_version",
            sa.String(length=20),
            nullable=False,
            server_default="1.0",
        ),
    )


def downgrade() -> None:
    op.drop_column("assistant_messages", "content_schema_version")
