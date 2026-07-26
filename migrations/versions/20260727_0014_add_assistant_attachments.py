"""add product-owned platform-assistant attachments

Revision ID: 20260727_0014
Revises: 20260727_0013
Create Date: 2026-07-27 06:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0014"
down_revision: str | Sequence[str] | None = "20260727_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_attachments",
        sa.Column("attachment_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_relative_path", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("attached_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('uploaded', 'attached', 'revoked')",
            name="valid_status",
        ),
        sa.CheckConstraint("byte_size >= 0", name="nonnegative_byte_size"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["assistant_conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["assistant_messages.message_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("attachment_id"),
        sa.UniqueConstraint("storage_relative_path"),
    )
    op.create_index(
        "ix_assistant_attachments_conversation_id",
        "assistant_attachments",
        ["conversation_id"],
    )
    op.create_index(
        "ix_assistant_attachments_owner_user_id",
        "assistant_attachments",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_assistant_attachments_message_id",
        "assistant_attachments",
        ["message_id"],
    )
    op.create_index(
        "ix_assistant_attachments_sha256",
        "assistant_attachments",
        ["sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_attachments_sha256", table_name="assistant_attachments")
    op.drop_index("ix_assistant_attachments_message_id", table_name="assistant_attachments")
    op.drop_index(
        "ix_assistant_attachments_owner_user_id",
        table_name="assistant_attachments",
    )
    op.drop_index(
        "ix_assistant_attachments_conversation_id",
        table_name="assistant_attachments",
    )
    op.drop_table("assistant_attachments")
